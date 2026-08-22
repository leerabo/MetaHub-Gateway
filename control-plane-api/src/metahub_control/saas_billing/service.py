"""Database-authoritative PRD-098 SaaS catalog and subscription services."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import uuid4

from metahub_shared.db.connection import connect_database
from metahub_shared.db.errors import DbError
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError

from metahub_control.platform_access.service import (
    PlatformContext,
    authenticate_platform_session_in_transaction,
)
from metahub_control.workspace_access.service import (
    WorkspaceSessionContext,
    authenticate_workspace_session_in_transaction,
)

from .models import (
    EntitlementDefinition,
    EntitlementSnapshotInfo,
    SaasPlanCatalogInfo,
    SaasPlanCatalogResponse,
    SaasPlanCreateRequest,
    SaasPlanVersionInfo,
    SubscriptionChangeRequest,
    SubscriptionCreateRequest,
    SubscriptionInfo,
    SubscriptionOrderInfo,
    SubscriptionResponse,
    TrialInfo,
)

_PLAN_CODE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,39}$")
_PLAN_VERSION_ID_RE = re.compile(r"^spv_[0-9a-f]{32}$")
_IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$")
_FINANCE_ROLES = frozenset({"owner", "finance_admin"})


class _SubscriptionActor(Protocol):
    workspace_id: str
    tenant_id: str
    membership_id: str


@dataclass(frozen=True, slots=True)
class _ScheduledTrialActor:
    workspace_id: str
    tenant_id: str
    membership_id: str


def list_public_plans(
    config: AppConfig, *, request_id: str, trace_id: str
) -> SaasPlanCatalogResponse:
    """Return only the latest published version of each active catalog."""

    connection = _connect(config)
    try:
        connection.execute("BEGIN")
        _require_schema(connection)
        rows = connection.execute(
            """
            SELECT catalog.plan_catalog_id,catalog.plan_code,catalog.display_name,
              catalog.status catalog_status,catalog.display_order,catalog.revision,
              version.plan_version_id,version.version_number,version.status version_status,
              version.billing_period,version.currency,version.unit_price_minor,
              version.trial_days,version.entitlements_json,version.entitlements_sha256,
              version.published_at,version.created_at version_created_at
            FROM saas_plan_catalogs catalog
            JOIN saas_plan_versions version
              ON version.plan_catalog_id=catalog.plan_catalog_id
            WHERE catalog.status='active' AND version.status='published'
              AND version.version_number=(
                SELECT MAX(candidate.version_number) FROM saas_plan_versions candidate
                WHERE candidate.plan_catalog_id=catalog.plan_catalog_id
                  AND candidate.status='published')
            ORDER BY catalog.display_order,catalog.plan_code
            """
        ).fetchall()
        connection.commit()
        return SaasPlanCatalogResponse(
            plans=tuple(_catalogs_from_joined(rows)),
            request_id=request_id,
            trace_id=trace_id,
        )
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, KeyError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("saas_catalog.unavailable") from None
    finally:
        connection.close()


def list_platform_plans(
    config: AppConfig,
    *,
    session_token: str,
    request_id: str,
    trace_id: str,
) -> SaasPlanCatalogResponse:
    connection = _connect(config)
    try:
        connection.execute("BEGIN")
        actor = authenticate_platform_session_in_transaction(
            connection, config, session_token=session_token, touch=True
        )
        _require_platform_permission(actor, "platform.catalog.read")
        _require_schema(connection)
        rows = connection.execute(
            """
            SELECT catalog.plan_catalog_id,catalog.plan_code,catalog.display_name,
              catalog.status catalog_status,catalog.display_order,catalog.revision,
              version.plan_version_id,version.version_number,version.status version_status,
              version.billing_period,version.currency,version.unit_price_minor,
              version.trial_days,version.entitlements_json,version.entitlements_sha256,
              version.published_at,version.created_at version_created_at
            FROM saas_plan_catalogs catalog
            LEFT JOIN saas_plan_versions version
              ON version.plan_catalog_id=catalog.plan_catalog_id
            ORDER BY catalog.display_order,catalog.plan_code,version.version_number DESC
            """
        ).fetchall()
        connection.commit()
        return SaasPlanCatalogResponse(
            plans=tuple(_catalogs_from_joined(rows)),
            request_id=request_id,
            trace_id=trace_id,
        )
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, KeyError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("saas_catalog.unavailable") from None
    finally:
        connection.close()


def create_platform_plan(
    config: AppConfig,
    *,
    session_token: str,
    csrf_token: str,
    payload: SaasPlanCreateRequest,
    request_id: str,
    trace_id: str,
) -> SaasPlanCatalogResponse:
    plan_code = _plan_code(payload.plan_code)
    display_name = _display_name(payload.display_name)
    unit_price_minor = _money_to_minor(payload.unit_price)
    if payload.billing_period == "none" and unit_price_minor != 0:
        raise ApiError("saas_catalog.request_invalid")
    entitlements_json = _canonical_entitlements(payload.entitlements)
    entitlements_sha256 = _sha256(entitlements_json)
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
        _require_platform_permission(actor, "platform.catalog.write")
        _require_schema(connection)
        now = _utcnow()
        stamp = _db_time(now)
        catalog = connection.execute(
            "SELECT * FROM saas_plan_catalogs WHERE plan_code=?", (plan_code,)
        ).fetchone()
        if catalog is None:
            catalog_id = _id("spc_")
            connection.execute(
                """
                INSERT INTO saas_plan_catalogs(
                  plan_catalog_id,plan_code,display_name,status,display_order,revision,
                  created_by_operator_id,created_at,updated_at)
                VALUES(?,?,?,?,?,1,?,?,?)
                """,
                (
                    catalog_id,
                    plan_code,
                    display_name,
                    "active" if payload.publish else "draft",
                    payload.display_order,
                    actor.operator_id,
                    stamp,
                    stamp,
                ),
            )
        else:
            if (
                catalog["status"] == "retired"
                or catalog["display_name"] != display_name
                or int(catalog["display_order"]) != payload.display_order
            ):
                raise ApiError("saas_catalog.conflict")
            catalog_id = str(catalog["plan_catalog_id"])
            if payload.publish and catalog["status"] == "draft":
                connection.execute(
                    """
                    UPDATE saas_plan_catalogs
                    SET status='active',revision=revision+1,updated_at=?
                    WHERE plan_catalog_id=? AND status='draft'
                    """,
                    (stamp, catalog_id),
                )
        version_number = int(
            connection.execute(
                "SELECT COALESCE(MAX(version_number),0)+1 FROM saas_plan_versions "
                "WHERE plan_catalog_id=?",
                (catalog_id,),
            ).fetchone()[0]
        )
        version_id = _id("spv_")
        connection.execute(
            """
            INSERT INTO saas_plan_versions(
              plan_version_id,plan_catalog_id,version_number,status,billing_period,currency,
              unit_price_minor,trial_days,entitlements_json,entitlements_sha256,
              created_by_operator_id,published_at,created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                version_id,
                catalog_id,
                version_number,
                "published" if payload.publish else "draft",
                payload.billing_period,
                payload.currency,
                unit_price_minor,
                payload.trial_days,
                entitlements_json,
                entitlements_sha256,
                actor.operator_id,
                stamp if payload.publish else None,
                stamp,
            ),
        )
        _platform_audit(
            connection,
            actor=actor,
            action="saas.plan_version_created",
            object_type="saas_plan_version",
            object_id=version_id,
            request_id=request_id,
            trace_id=trace_id,
            after={
                "plan_code": plan_code,
                "version_number": version_number,
                "published": payload.publish,
                "currency": payload.currency,
                "unit_price_minor": unit_price_minor,
                "entitlements_sha256": entitlements_sha256,
            },
            now=now,
        )
        rows = connection.execute(
            """
            SELECT catalog.plan_catalog_id,catalog.plan_code,catalog.display_name,
              catalog.status catalog_status,catalog.display_order,catalog.revision,
              version.plan_version_id,version.version_number,version.status version_status,
              version.billing_period,version.currency,version.unit_price_minor,
              version.trial_days,version.entitlements_json,version.entitlements_sha256,
              version.published_at,version.created_at version_created_at
            FROM saas_plan_catalogs catalog
            LEFT JOIN saas_plan_versions version
              ON version.plan_catalog_id=catalog.plan_catalog_id
            WHERE catalog.plan_catalog_id=? ORDER BY version.version_number DESC
            """,
            (catalog_id,),
        ).fetchall()
        connection.commit()
        return SaasPlanCatalogResponse(
            plans=tuple(_catalogs_from_joined(rows)),
            request_id=request_id,
            trace_id=trace_id,
        )
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, KeyError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("saas_catalog.unavailable") from None
    finally:
        connection.close()


def get_current_subscription(
    config: AppConfig,
    *,
    session_token: str,
    request_id: str,
    trace_id: str,
) -> SubscriptionResponse:
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        actor = authenticate_workspace_session_in_transaction(
            connection, config, session_token=session_token, touch=True
        )
        _require_finance(actor)
        _require_schema(connection)
        _expire_trial_if_due(
            connection,
            actor,
            _utcnow(),
            request_id=request_id,
            trace_id=trace_id,
        )
        response = _subscription_response(
            connection,
            actor.workspace_id,
            request_id=request_id,
            trace_id=trace_id,
        )
        connection.commit()
        return response
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, KeyError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("subscription.unavailable") from None
    finally:
        connection.close()


def expire_due_trials(config: AppConfig, *, limit: int = 50) -> int:
    """Expire a bounded batch with the same state machine used by request guards."""

    if type(limit) is not int or not 1 <= limit <= 200:
        raise ApiError("subscription.request_invalid")
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        _require_schema(connection)
        now = _utcnow()
        rows = connection.execute(
            """
            SELECT trial.workspace_id,trial.tenant_id,
              subscription.created_by_membership_id
            FROM subscription_trials trial
            JOIN workspace_subscriptions subscription
              ON subscription.subscription_id=trial.subscription_id
            WHERE trial.status='active' AND subscription.status='trialing'
              AND trial.ends_at<=?
            ORDER BY trial.ends_at,trial.trial_id
            LIMIT ?
            """,
            (_db_time(now), limit),
        ).fetchall()
        correlation_id = f"saas-trial-expiry-{uuid4().hex}"
        expired = 0
        for row in rows:
            actor = _ScheduledTrialActor(
                workspace_id=str(row["workspace_id"]),
                tenant_id=str(row["tenant_id"]),
                membership_id=str(row["created_by_membership_id"]),
            )
            if _expire_trial_if_due(
                connection,
                actor,
                now,
                request_id=correlation_id,
                trace_id=correlation_id,
            ):
                expired += 1
        connection.commit()
        return expired
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, KeyError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("subscription.unavailable") from None
    finally:
        connection.close()


def create_subscription(
    config: AppConfig,
    *,
    session_token: str,
    csrf_token: str,
    payload: SubscriptionCreateRequest,
    idempotency_key: str,
    request_id: str,
    trace_id: str,
) -> SubscriptionResponse:
    _validate_plan_version_id(payload.plan_version_id)
    _validate_idempotency(idempotency_key)
    request_sha256 = _intent_hash(
        "create", {"plan_version_id": payload.plan_version_id, "start_trial": payload.start_trial}
    )
    key_sha256 = _sha256(idempotency_key)
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        actor = authenticate_workspace_session_in_transaction(
            connection,
            config,
            session_token=session_token,
            csrf_token=csrf_token,
            touch=False,
        )
        _require_finance(actor)
        _require_schema(connection)
        replay = _order_replay(connection, actor.workspace_id, key_sha256, request_sha256)
        if replay:
            response = _subscription_response(
                connection,
                actor.workspace_id,
                request_id=request_id,
                trace_id=trace_id,
                replay=True,
            )
            connection.commit()
            return response
        if connection.execute(
            "SELECT 1 FROM workspace_subscriptions WHERE workspace_id=?",
            (actor.workspace_id,),
        ).fetchone():
            raise ApiError("subscription.conflict")
        plan = _published_plan(connection, payload.plan_version_id)
        if payload.start_trial and int(plan["trial_days"]) == 0:
            raise ApiError("subscription.conflict")
        now = _utcnow()
        stamp = _db_time(now)
        period_end = _period_end(now, str(plan["billing_period"]))
        trial_end = (
            now + timedelta(days=int(plan["trial_days"])) if payload.start_trial else None
        )
        status = (
            "trialing"
            if trial_end is not None
            else "active"
            if int(plan["unit_price_minor"]) == 0
            else "pending"
        )
        subscription_id = _id("sub_")
        connection.execute(
            """
            INSERT INTO workspace_subscriptions(
              subscription_id,workspace_id,tenant_id,plan_version_id,pending_plan_version_id,
              status,current_period_started_at,current_period_ends_at,pending_effective_at,
              state_revision,created_by_membership_id,created_at,updated_at)
            VALUES(?,?,?,?,NULL,?,?,?,NULL,1,?,?,?)
            """,
            (
                subscription_id,
                actor.workspace_id,
                actor.tenant_id,
                plan["plan_version_id"],
                status,
                stamp,
                _db_time(period_end) if period_end else None,
                actor.membership_id,
                stamp,
                stamp,
            ),
        )
        order_id = _id("sor_")
        order_status = "not_required" if status in {"active", "trialing"} else "pending_payment"
        connection.execute(
            """
            INSERT INTO subscription_order_records(
              order_record_id,workspace_id,tenant_id,subscription_id,plan_version_id,
              order_kind,status,currency,amount_minor,idempotency_sha256,request_sha256,
              created_by_membership_id,created_at)
            VALUES(?,?,?,?,?,'create',?,?,?,?,?,?,?)
            """,
            (
                order_id,
                actor.workspace_id,
                actor.tenant_id,
                subscription_id,
                plan["plan_version_id"],
                order_status,
                plan["currency"],
                int(plan["unit_price_minor"]),
                key_sha256,
                request_sha256,
                actor.membership_id,
                stamp,
            ),
        )
        if trial_end is not None:
            connection.execute(
                """
                INSERT INTO subscription_trials(
                  trial_id,workspace_id,tenant_id,subscription_id,plan_version_id,cycle,status,
                  starts_at,ends_at,created_at)
                VALUES(?,?,?,?,?,1,'active',?,?,?)
                """,
                (
                    _id("trl_"),
                    actor.workspace_id,
                    actor.tenant_id,
                    subscription_id,
                    plan["plan_version_id"],
                    stamp,
                    _db_time(trial_end),
                    stamp,
                ),
            )
        if status in {"active", "trialing"}:
            _insert_entitlement_snapshot(
                connection,
                actor=actor,
                subscription_id=subscription_id,
                plan=plan,
                order_id=order_id,
                effective_from=now,
                effective_until=trial_end if trial_end is not None else period_end,
            )
        _subscription_event(
            connection,
            actor=actor,
            subscription_id=subscription_id,
            event_type="subscription.created",
            reason_code="customer_selected",
            request_id=request_id,
            trace_id=trace_id,
            payload={
                "schema_version": 1,
                "status": status,
                "plan_version_id": plan["plan_version_id"],
            },
            now=now,
        )
        response = _subscription_response(
            connection,
            actor.workspace_id,
            request_id=request_id,
            trace_id=trace_id,
        )
        connection.commit()
        return response
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, KeyError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("subscription.unavailable") from None
    finally:
        connection.close()


def change_subscription_plan(
    config: AppConfig,
    *,
    session_token: str,
    csrf_token: str,
    payload: SubscriptionChangeRequest,
    expected_revision: int,
    idempotency_key: str,
    request_id: str,
    trace_id: str,
) -> SubscriptionResponse:
    _validate_plan_version_id(payload.plan_version_id)
    _validate_idempotency(idempotency_key)
    request_sha256 = _intent_hash(
        "change", {"plan_version_id": payload.plan_version_id, "revision": expected_revision}
    )
    key_sha256 = _sha256(idempotency_key)
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        actor = authenticate_workspace_session_in_transaction(
            connection,
            config,
            session_token=session_token,
            csrf_token=csrf_token,
            touch=False,
        )
        _require_finance(actor)
        _require_schema(connection)
        replay = _order_replay(connection, actor.workspace_id, key_sha256, request_sha256)
        if replay:
            response = _subscription_response(
                connection,
                actor.workspace_id,
                request_id=request_id,
                trace_id=trace_id,
                replay=True,
            )
            connection.commit()
            return response
        subscription = connection.execute(
            "SELECT * FROM workspace_subscriptions WHERE workspace_id=?",
            (actor.workspace_id,),
        ).fetchone()
        if subscription is None:
            raise ApiError("subscription.not_found")
        if int(subscription["state_revision"]) != expected_revision:
            raise ApiError("subscription.revision_conflict")
        if (
            subscription["status"] in {"canceled", "expired"}
            or subscription["plan_version_id"] == payload.plan_version_id
            or subscription["pending_plan_version_id"] is not None
        ):
            raise ApiError("subscription.conflict")
        current = connection.execute(
            "SELECT * FROM saas_plan_versions WHERE plan_version_id=?",
            (subscription["plan_version_id"],),
        ).fetchone()
        target = _published_plan(connection, payload.plan_version_id)
        if current is None:
            raise ApiError("subscription.unavailable")
        downgrade = (
            current["currency"] == target["currency"]
            and int(target["unit_price_minor"]) <= int(current["unit_price_minor"])
        )
        now = _utcnow()
        effective = (
            _parse_time(subscription["current_period_ends_at"])
            if subscription["current_period_ends_at"] is not None
            else now
        )
        connection.execute(
            """
            UPDATE workspace_subscriptions
            SET pending_plan_version_id=?,pending_effective_at=?,state_revision=state_revision+1,
              updated_at=?
            WHERE subscription_id=? AND state_revision=?
            """,
            (
                target["plan_version_id"],
                _db_time(effective),
                _db_time(now),
                subscription["subscription_id"],
                expected_revision,
            ),
        )
        order_id = _id("sor_")
        connection.execute(
            """
            INSERT INTO subscription_order_records(
              order_record_id,workspace_id,tenant_id,subscription_id,plan_version_id,
              order_kind,status,currency,amount_minor,idempotency_sha256,request_sha256,
              created_by_membership_id,created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                order_id,
                actor.workspace_id,
                actor.tenant_id,
                subscription["subscription_id"],
                target["plan_version_id"],
                "downgrade" if downgrade else "upgrade",
                "scheduled" if downgrade else "pending_payment",
                target["currency"],
                int(target["unit_price_minor"]),
                key_sha256,
                request_sha256,
                actor.membership_id,
                _db_time(now),
            ),
        )
        _subscription_event(
            connection,
            actor=actor,
            subscription_id=str(subscription["subscription_id"]),
            event_type="subscription.plan_change_scheduled",
            reason_code="customer_changed_plan",
            request_id=request_id,
            trace_id=trace_id,
            payload={
                "schema_version": 1,
                "target_plan_version_id": target["plan_version_id"],
                "change_kind": "downgrade" if downgrade else "upgrade",
            },
            now=now,
        )
        response = _subscription_response(
            connection,
            actor.workspace_id,
            request_id=request_id,
            trace_id=trace_id,
        )
        connection.commit()
        return response
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, KeyError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("subscription.unavailable") from None
    finally:
        connection.close()


def _catalogs_from_joined(rows: list[sqlite3.Row]) -> list[SaasPlanCatalogInfo]:
    grouped: dict[str, dict[str, object]] = {}
    for row in rows:
        catalog_id = str(row["plan_catalog_id"])
        item = grouped.setdefault(
            catalog_id,
            {
                "plan_catalog_id": catalog_id,
                "plan_code": row["plan_code"],
                "display_name": row["display_name"],
                "status": row["catalog_status"],
                "display_order": int(row["display_order"]),
                "revision": int(row["revision"]),
                "versions": [],
            },
        )
        if row["plan_version_id"] is not None:
            item["versions"].append(_plan_version(row))  # type: ignore[union-attr]
    result: list[SaasPlanCatalogInfo] = []
    for item in grouped.values():
        result.append(
            SaasPlanCatalogInfo(
                plan_catalog_id=str(item["plan_catalog_id"]),
                plan_code=str(item["plan_code"]),
                display_name=str(item["display_name"]),
                status=item["status"],  # type: ignore[arg-type]
                display_order=int(item["display_order"]),
                revision=int(item["revision"]),
                versions=tuple(item["versions"]),  # type: ignore[arg-type]
            )
        )
    return result


def _plan_version(row: sqlite3.Row) -> SaasPlanVersionInfo:
    return SaasPlanVersionInfo(
        plan_version_id=row["plan_version_id"],
        version_number=int(row["version_number"]),
        status=row["version_status"],
        billing_period=row["billing_period"],
        currency=row["currency"],
        unit_price=_minor_to_money(int(row["unit_price_minor"])),
        trial_days=int(row["trial_days"]),
        entitlements=EntitlementDefinition.model_validate(json.loads(row["entitlements_json"])),
        entitlements_sha256=row["entitlements_sha256"],
        published_at=_optional_time(row["published_at"]),
        created_at=_parse_time(row["version_created_at"]),
    )


def _subscription_response(
    connection: sqlite3.Connection,
    workspace_id: str,
    *,
    request_id: str,
    trace_id: str,
    replay: bool = False,
) -> SubscriptionResponse:
    row = connection.execute(
        "SELECT * FROM workspace_subscriptions WHERE workspace_id=?", (workspace_id,)
    ).fetchone()
    if row is None:
        return SubscriptionResponse(
            idempotent_replay=replay, request_id=request_id, trace_id=trace_id
        )
    trial = connection.execute(
        "SELECT * FROM subscription_trials WHERE subscription_id=? ORDER BY cycle DESC LIMIT 1",
        (row["subscription_id"],),
    ).fetchone()
    entitlement = connection.execute(
        """
        SELECT * FROM entitlement_snapshots
        WHERE subscription_id=? ORDER BY snapshot_version DESC LIMIT 1
        """,
        (row["subscription_id"],),
    ).fetchone()
    order = connection.execute(
        """
        SELECT * FROM subscription_order_records
        WHERE subscription_id=? ORDER BY created_at DESC,rowid DESC LIMIT 1
        """,
        (row["subscription_id"],),
    ).fetchone()
    return SubscriptionResponse(
        subscription=SubscriptionInfo(
            subscription_id=row["subscription_id"],
            workspace_id=row["workspace_id"],
            plan_version_id=row["plan_version_id"],
            pending_plan_version_id=row["pending_plan_version_id"],
            status=row["status"],
            current_period_started_at=_parse_time(row["current_period_started_at"]),
            current_period_ends_at=_optional_time(row["current_period_ends_at"]),
            pending_effective_at=_optional_time(row["pending_effective_at"]),
            state_revision=int(row["state_revision"]),
            created_at=_parse_time(row["created_at"]),
            updated_at=_parse_time(row["updated_at"]),
        ),
        trial=(
            TrialInfo(
                trial_id=trial["trial_id"],
                plan_version_id=trial["plan_version_id"],
                status=trial["status"],
                starts_at=_parse_time(trial["starts_at"]),
                ends_at=_parse_time(trial["ends_at"]),
            )
            if trial is not None
            else None
        ),
        entitlement=(
            EntitlementSnapshotInfo(
                entitlement_snapshot_id=entitlement["entitlement_snapshot_id"],
                plan_version_id=entitlement["plan_version_id"],
                snapshot_version=int(entitlement["snapshot_version"]),
                entitlements=EntitlementDefinition.model_validate(
                    json.loads(entitlement["entitlements_json"])
                ),
                entitlements_sha256=entitlement["entitlements_sha256"],
                effective_from=_parse_time(entitlement["effective_from"]),
                effective_until=_optional_time(entitlement["effective_until"]),
            )
            if entitlement is not None
            else None
        ),
        latest_order=(
            SubscriptionOrderInfo(
                order_record_id=order["order_record_id"],
                plan_version_id=order["plan_version_id"],
                order_kind=order["order_kind"],
                status=order["status"],
                currency=order["currency"],
                amount=_minor_to_money(int(order["amount_minor"])),
                created_at=_parse_time(order["created_at"]),
            )
            if order is not None
            else None
        ),
        idempotent_replay=replay,
        request_id=request_id,
        trace_id=trace_id,
    )


def _expire_trial_if_due(
    connection: sqlite3.Connection,
    actor: _SubscriptionActor,
    now: datetime,
    *,
    request_id: str,
    trace_id: str,
) -> bool:
    trial = connection.execute(
        """
        SELECT trial.*,subscription.status subscription_status,version.unit_price_minor
        FROM subscription_trials trial
        JOIN workspace_subscriptions subscription
          ON subscription.subscription_id=trial.subscription_id
        JOIN saas_plan_versions version ON version.plan_version_id=trial.plan_version_id
        WHERE trial.workspace_id=? AND trial.status='active'
        """,
        (actor.workspace_id,),
    ).fetchone()
    if trial is None or _parse_time(trial["ends_at"]) > now:
        return False
    if trial["subscription_status"] != "trialing":
        raise ApiError("subscription.unavailable")
    connection.execute(
        "UPDATE subscription_trials SET status='expired' WHERE trial_id=? AND status='active'",
        (trial["trial_id"],),
    )
    fallback = connection.execute(
        """
        SELECT version.* FROM saas_plan_versions version
        JOIN saas_plan_catalogs catalog ON catalog.plan_catalog_id=version.plan_catalog_id
        WHERE catalog.status='active' AND version.status='published'
          AND version.unit_price_minor=0
          AND version.version_number=(
            SELECT MAX(candidate.version_number) FROM saas_plan_versions candidate
            WHERE candidate.plan_catalog_id=version.plan_catalog_id
              AND candidate.status='published')
        ORDER BY catalog.display_order,catalog.plan_code LIMIT 1
        """
    ).fetchone()
    if fallback is None:
        updated = connection.execute(
            """
            UPDATE workspace_subscriptions
            SET status='expired',pending_plan_version_id=NULL,pending_effective_at=NULL,
              state_revision=state_revision+1,updated_at=?
            WHERE subscription_id=? AND status='trialing'
            """,
            (_db_time(now), trial["subscription_id"]),
        ).rowcount
        if updated != 1:
            raise ApiError("subscription.unavailable")
        _subscription_event(
            connection,
            actor=actor,
            subscription_id=str(trial["subscription_id"]),
            event_type="subscription.trial_expired",
            reason_code="trial_expired_no_free_plan",
            request_id=request_id,
            trace_id=trace_id,
            payload={"schema_version": 1, "fallback_plan_version_id": None},
            now=now,
        )
        return True

    effective_from = _parse_time(trial["ends_at"])
    period_end = _period_end(effective_from, str(fallback["billing_period"]))
    updated = connection.execute(
        """
        UPDATE workspace_subscriptions
        SET plan_version_id=?,pending_plan_version_id=NULL,status='active',
          current_period_started_at=?,current_period_ends_at=?,pending_effective_at=NULL,
          state_revision=state_revision+1,updated_at=?
        WHERE subscription_id=? AND status='trialing'
        """,
        (
            fallback["plan_version_id"],
            _db_time(effective_from),
            _db_time(period_end) if period_end else None,
            _db_time(now),
            trial["subscription_id"],
        ),
    ).rowcount
    if updated != 1:
        raise ApiError("subscription.unavailable")
    order_id = _id("sor_")
    key_sha256 = _sha256(f"trial-fallback:{trial['trial_id']}")
    request_sha256 = _intent_hash(
        "trial_fallback",
        {
            "trial_id": trial["trial_id"],
            "plan_version_id": fallback["plan_version_id"],
        },
    )
    connection.execute(
        """
        INSERT INTO subscription_order_records(
          order_record_id,workspace_id,tenant_id,subscription_id,plan_version_id,
          order_kind,status,currency,amount_minor,idempotency_sha256,request_sha256,
          created_by_membership_id,created_at)
        VALUES(?,?,?,?,?,'downgrade','not_required',?,0,?,?,?,?)
        """,
        (
            order_id,
            actor.workspace_id,
            actor.tenant_id,
            trial["subscription_id"],
            fallback["plan_version_id"],
            fallback["currency"],
            key_sha256,
            request_sha256,
            actor.membership_id,
            _db_time(now),
        ),
    )
    _insert_entitlement_snapshot(
        connection,
        actor=actor,
        subscription_id=str(trial["subscription_id"]),
        plan=fallback,
        order_id=order_id,
        effective_from=effective_from,
        effective_until=period_end,
    )
    _subscription_event(
        connection,
        actor=actor,
        subscription_id=str(trial["subscription_id"]),
        event_type="subscription.trial_expired",
        reason_code="trial_expired_fallback",
        request_id=request_id,
        trace_id=trace_id,
        payload={
            "schema_version": 1,
            "fallback_plan_version_id": fallback["plan_version_id"],
        },
        now=now,
    )
    return True


def _published_plan(connection: sqlite3.Connection, plan_version_id: str) -> sqlite3.Row:
    row = connection.execute(
        """
        SELECT version.* FROM saas_plan_versions version
        JOIN saas_plan_catalogs catalog ON catalog.plan_catalog_id=version.plan_catalog_id
        WHERE version.plan_version_id=? AND version.status='published'
          AND catalog.status='active'
        """,
        (plan_version_id,),
    ).fetchone()
    if row is None:
        raise ApiError("saas_catalog.not_found")
    return row


def _insert_entitlement_snapshot(
    connection: sqlite3.Connection,
    *,
    actor: _SubscriptionActor,
    subscription_id: str,
    plan: sqlite3.Row,
    order_id: str,
    effective_from: datetime,
    effective_until: datetime | None,
) -> None:
    version = int(
        connection.execute(
            "SELECT COALESCE(MAX(snapshot_version),0)+1 FROM entitlement_snapshots "
            "WHERE subscription_id=?",
            (subscription_id,),
        ).fetchone()[0]
    )
    connection.execute(
        """
        INSERT INTO entitlement_snapshots(
          entitlement_snapshot_id,workspace_id,tenant_id,subscription_id,plan_version_id,
          source_order_record_id,snapshot_version,entitlements_json,entitlements_sha256,
          effective_from,effective_until,created_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            _id("ent_"),
            actor.workspace_id,
            actor.tenant_id,
            subscription_id,
            plan["plan_version_id"],
            order_id,
            version,
            plan["entitlements_json"],
            plan["entitlements_sha256"],
            _db_time(effective_from),
            _db_time(effective_until) if effective_until else None,
            _db_time(effective_from),
        ),
    )


def _order_replay(
    connection: sqlite3.Connection,
    workspace_id: str,
    key_sha256: str,
    request_sha256: str,
) -> bool:
    row = connection.execute(
        """
        SELECT request_sha256 FROM subscription_order_records
        WHERE workspace_id=? AND idempotency_sha256=?
        """,
        (workspace_id, key_sha256),
    ).fetchone()
    if row is None:
        return False
    if row["request_sha256"] != request_sha256:
        raise ApiError("subscription.idempotency_conflict")
    return True


def _subscription_event(
    connection: sqlite3.Connection,
    *,
    actor: _SubscriptionActor,
    subscription_id: str,
    event_type: str,
    reason_code: str,
    request_id: str,
    trace_id: str,
    payload: object,
    now: datetime,
) -> None:
    connection.execute(
        """
        INSERT INTO saas_subscription_events(
          event_id,workspace_id,tenant_id,subscription_id,actor_membership_id,event_type,
          reason_code,request_id,trace_id,payload_json,occurred_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            _id("sev_"),
            actor.workspace_id,
            actor.tenant_id,
            subscription_id,
            actor.membership_id,
            event_type,
            reason_code,
            request_id,
            trace_id,
            json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")),
            _db_time(now),
        ),
    )


def _platform_audit(
    connection: sqlite3.Connection,
    *,
    actor: PlatformContext,
    action: str,
    object_type: str,
    object_id: str,
    request_id: str,
    trace_id: str,
    after: object,
    now: datetime,
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
            actor.operator_id,
            action,
            object_type,
            object_id,
            "catalog_version_created",
            request_id,
            trace_id,
            _sha256(json.dumps(after, ensure_ascii=True, sort_keys=True, separators=(",", ":"))),
            _db_time(now),
        ),
    )


def _require_finance(actor: WorkspaceSessionContext) -> None:
    if actor.membership_role not in _FINANCE_ROLES:
        raise ApiError("workspace_session.permission_denied")


def _require_platform_permission(actor: PlatformContext, permission: str) -> None:
    if permission not in actor.permissions:
        raise ApiError("platform.permission_denied")


def _require_schema(connection: sqlite3.Connection) -> None:
    count = int(
        connection.execute(
            """
            SELECT COUNT(*) FROM sqlite_schema WHERE type='table' AND name IN(
              'saas_plan_catalogs','saas_plan_versions','workspace_subscriptions',
              'subscription_trials','subscription_order_records','entitlement_snapshots',
              'saas_subscription_events')
            """
        ).fetchone()[0]
    )
    if count != 7:
        raise ApiError("saas_catalog.unavailable")


def _canonical_entitlements(value: EntitlementDefinition) -> str:
    return json.dumps(
        value.model_dump(mode="json"), ensure_ascii=True, sort_keys=True, separators=(",", ":")
    )


def _plan_code(value: str) -> str:
    if type(value) is not str or _PLAN_CODE_RE.fullmatch(value) is None:
        raise ApiError("saas_catalog.request_invalid")
    return value


def _display_name(value: str) -> str:
    if type(value) is not str:
        raise ApiError("saas_catalog.request_invalid")
    normalized = unicodedata.normalize("NFKC", value).strip()
    if not 1 <= len(normalized) <= 80 or any(ord(character) < 32 for character in normalized):
        raise ApiError("saas_catalog.request_invalid")
    return normalized


def _money_to_minor(value: str) -> int:
    if type(value) is not str or re.fullmatch(r"(?:0|[1-9][0-9]{0,10})\.[0-9]{2}", value) is None:
        raise ApiError("saas_catalog.request_invalid")
    major, minor = value.split(".")
    amount = int(major) * 100 + int(minor)
    if amount > 1_000_000_000_000:
        raise ApiError("saas_catalog.request_invalid")
    return amount


def _minor_to_money(value: int) -> str:
    return f"{value // 100}.{value % 100:02d}"


def _validate_plan_version_id(value: str) -> None:
    if type(value) is not str or _PLAN_VERSION_ID_RE.fullmatch(value) is None:
        raise ApiError("subscription.request_invalid")


def _validate_idempotency(value: str) -> None:
    if type(value) is not str or _IDEMPOTENCY_RE.fullmatch(value) is None:
        raise ApiError("subscription.request_invalid")


def _intent_hash(operation: str, payload: object) -> str:
    return _sha256(
        json.dumps([operation, payload], ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    )


def _period_end(start: datetime, period: str) -> datetime | None:
    if period == "none":
        return None
    return start + timedelta(days=365 if period == "yearly" else 30)


def _connect(config: AppConfig) -> sqlite3.Connection:
    try:
        return connect_database(config.database_url)
    except DbError as exc:
        raise ApiError("saas_catalog.unavailable") from exc


def _rollback(connection: sqlite3.Connection) -> None:
    if connection.in_transaction:
        connection.rollback()


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _db_time(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_time(value: object) -> datetime:
    if type(value) is not str:
        raise ValueError("invalid time")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _optional_time(value: object) -> datetime | None:
    return None if value is None else _parse_time(value)


def _id(prefix: str) -> str:
    return prefix + uuid4().hex


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
