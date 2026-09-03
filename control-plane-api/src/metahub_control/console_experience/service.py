"""Database-authoritative console projections for PRD-145."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import sqlite3
import time
from collections import defaultdict
from datetime import UTC, datetime
from uuid import uuid4

from metahub_shared.db.connection import connect_database
from metahub_shared.db.errors import DbError
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError

from metahub_control.identity.service import authenticate_global_session_in_transaction
from metahub_control.platform_access.service import (
    PlatformContext,
    authenticate_platform_session_in_transaction,
)
from metahub_control.workspace_access.service import (
    WorkspaceSessionContext,
    authenticate_workspace_session_in_transaction,
)

from .models import (
    AccountPreferenceCommandResponse,
    AccountPreferencePatch,
    AccountPreferenceResponse,
    ApprovalQueueItem,
    ApprovalQueueResponse,
    AssistantCitation,
    AssistantClaim,
    AssistantQueryResponse,
    AssistantSourceStatus,
    ConsoleMetric,
    ConsoleSection,
    DeploymentDiagnostic,
    DeploymentSummaryResponse,
    OverviewResponse,
)

_PLATFORM_SOURCE_PERMISSIONS = {
    "status": "platform.status.read",
    "observability": "platform.observability.read",
    "jobs": "platform.job.read",
    "capacity": "platform.capacity.read",
    "channel": "platform.channel.statement.read",
    "customer_success": "platform.customer_success.read",
    "growth": "platform.growth.read",
    "branding": "platform.brand.read",
    "provider_engineering": "platform.provider_adapter.read",
    "commercial_accounting": "platform.commercial.plan.read",
}
_WORKSPACE_SOURCE_PERMISSIONS = {
    "role_tasks": "console.basic.read",
    "gateway_health": "edge.health.read",
    "provider_health": "provider.read",
    "governance": "dlp.read",
    "approvals": "approval.read",
    "usage_cost": "billing.usage.read",
    "notifications": "console.basic.read",
    "deployment": "edge.deployment.read",
}
_PLATFORM_CITATIONS = {
    source: (f"console.source.{source}", {
        "status": "status-incidents",
        "observability": "observability",
        "jobs": "job-runtime",
        "capacity": "capacity",
        "channel": "channels",
        "customer_success": "customer-success",
        "growth": "growth",
        "branding": "branding",
        "provider_engineering": "provider-engineering",
        "commercial_accounting": "commercial-accounting",
    }[source])
    for source in _PLATFORM_SOURCE_PERMISSIONS
}
_WORKSPACE_CITATIONS = {
    source: (f"console.source.{source}", {
        "role_tasks": "overview",
        "gateway_health": "private-gateways",
        "provider_health": "provider-health",
        "governance": "dlp-rules",
        "approvals": "approvals",
        "usage_cost": "usage",
        "notifications": "overview",
        "deployment": "deployment-summary",
    }[source])
    for source in _WORKSPACE_SOURCE_PERMISSIONS
}
_QUERY_SOURCES = {
    "operations_summary": frozenset(
        ("status", "observability", "jobs", "capacity", "role_tasks", "gateway_health")
    ),
    "security_summary": frozenset(
        ("status", "observability", "governance", "approvals")
    ),
    "billing_summary": frozenset(("commercial_accounting", "usage_cost")),
    "provider_summary": frozenset(
        ("provider_engineering", "provider_health", "gateway_health")
    ),
    "unknown": frozenset(),
}
_QUERY_KEYWORDS = {
    "security_summary": ("security", "risk", "安全", "风险", "合规"),
    "billing_summary": ("billing", "cost", "invoice", "费用", "成本", "账单"),
    "provider_summary": ("provider", "gateway", "模型", "网关", "供应商"),
    "operations_summary": ("status", "operation", "health", "状态", "运营", "健康"),
}
_APPROVAL_FILTERS = {
    "status": frozenset(("pending", "approved", "rejected", "expired")),
    "risk": frozenset(("standard", "high", "critical")),
}
_CURSOR_TTL_SECONDS = 300


def get_platform_overview(
    config: AppConfig,
    *,
    session_token: str,
    request_id: str,
    trace_id: str,
) -> OverviewResponse:
    connection = _connect(config, "platform.unavailable")
    try:
        connection.execute("BEGIN")
        actor = authenticate_platform_session_in_transaction(
            connection, config, session_token=session_token, touch=False
        )
        _require_platform(actor, "platform.console.overview.read")
        version = connection.execute(
            """SELECT version.* FROM platform_dashboard_projection_heads head
               JOIN platform_dashboard_projection_versions version
                 USING(platform_dashboard_projection_id,generation)
              WHERE head.head_key='platform'"""
        ).fetchone()
        if version is None:
            _write_read_audit(
                connection,
                config,
                actor_domain="platform",
                actor_id=actor.operator_id,
                owner=("platform",),
                action="platform.overview.read",
                permission_revision=actor.operator_revision,
                fingerprint=_platform_fingerprint(actor),
                watermark="none",
                outcome="unavailable",
                request_id=request_id,
                trace_id=trace_id,
            )
            connection.commit()
            raise ApiError("platform.unavailable")
        rows = connection.execute(
            """SELECT * FROM platform_dashboard_projection_sources
               WHERE platform_dashboard_projection_id=?
               ORDER BY source_code,metric_code""",
            (version["platform_dashboard_projection_id"],),
        ).fetchall()
        sections = _sections(
            rows,
            permissions=frozenset(actor.permissions),
            requirements=_PLATFORM_SOURCE_PERMISSIONS,
        )
        result = OverviewResponse(
            domain="platform",
            status=version["status"],
            generation=int(version["generation"]),
            navigation_epoch=max(1, actor.operator_revision),
            observed_at=_parse_time(version["observed_at"]),
            sections=sections,
            permission_revision=actor.operator_revision,
            permission_fingerprint=_platform_fingerprint(actor),
            request_id=request_id,
            trace_id=trace_id,
        )
        _write_read_audit(
            connection,
            config,
            actor_domain="platform",
            actor_id=actor.operator_id,
            owner=("platform",),
            action="platform.overview.read",
            permission_revision=actor.operator_revision,
            fingerprint=result.permission_fingerprint,
            watermark=f"generation:{result.generation}",
            outcome=_read_outcome(result.status, bool(result.sections)),
            request_id=request_id,
            trace_id=trace_id,
        )
        connection.commit()
        return result
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, DbError, ValueError, TypeError):
        _rollback(connection)
        raise ApiError("platform.unavailable") from None
    finally:
        connection.close()


def get_workspace_overview(
    config: AppConfig,
    *,
    session_token: str,
    request_id: str,
    trace_id: str,
) -> OverviewResponse:
    connection = _connect(config, "workspace_session.unavailable")
    try:
        connection.execute("BEGIN")
        actor = authenticate_workspace_session_in_transaction(
            connection, config, session_token=session_token, touch=False
        )
        _require_workspace(actor, "workspace.console.overview.read")
        version = connection.execute(
            """SELECT version.* FROM workspace_dashboard_projection_heads head
               JOIN workspace_dashboard_projection_versions version
                 ON version.workspace_id=head.workspace_id
                AND version.tenant_id=head.tenant_id
                AND version.workspace_dashboard_projection_id=
                    head.workspace_dashboard_projection_id
                AND version.generation=head.generation
              WHERE head.workspace_id=? AND head.tenant_id=?""",
            (actor.workspace_id, actor.tenant_id),
        ).fetchone()
        if version is None:
            _write_read_audit(
                connection,
                config,
                actor_domain="workspace",
                actor_id=actor.account_id,
                owner=(actor.workspace_id, actor.tenant_id),
                action="workspace.overview.read",
                permission_revision=actor.permission_revision,
                fingerprint=actor.authorization_fingerprint,
                watermark="none",
                outcome="unavailable",
                request_id=request_id,
                trace_id=trace_id,
            )
            connection.commit()
            raise ApiError("workspace_session.unavailable")
        rows = connection.execute(
            """SELECT * FROM workspace_dashboard_projection_sources
               WHERE workspace_id=? AND tenant_id=?
                 AND workspace_dashboard_projection_id=?
               ORDER BY source_code,metric_code""",
            (
                actor.workspace_id,
                actor.tenant_id,
                version["workspace_dashboard_projection_id"],
            ),
        ).fetchall()
        sections = _sections(
            rows,
            permissions=frozenset(actor.permission_codes),
            requirements=_WORKSPACE_SOURCE_PERMISSIONS,
        )
        result = OverviewResponse(
            domain="workspace",
            status=version["status"],
            generation=int(version["generation"]),
            navigation_epoch=max(1, actor.permission_revision),
            observed_at=_parse_time(version["observed_at"]),
            sections=sections,
            permission_revision=actor.permission_revision,
            permission_fingerprint=actor.authorization_fingerprint,
            request_id=request_id,
            trace_id=trace_id,
        )
        _write_read_audit(
            connection,
            config,
            actor_domain="workspace",
            actor_id=actor.account_id,
            owner=(actor.workspace_id, actor.tenant_id),
            action="workspace.overview.read",
            permission_revision=actor.permission_revision,
            fingerprint=actor.authorization_fingerprint,
            watermark=f"generation:{result.generation}",
            outcome=_read_outcome(result.status, bool(result.sections)),
            request_id=request_id,
            trace_id=trace_id,
        )
        connection.commit()
        return result
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, DbError, ValueError, TypeError):
        _rollback(connection)
        raise ApiError("workspace_session.unavailable") from None
    finally:
        connection.close()


def query_platform_assistant(
    config: AppConfig,
    *,
    session_token: str,
    question: str,
    request_id: str,
    trace_id: str,
) -> AssistantQueryResponse:
    started_ns = time.perf_counter_ns()
    connection = _connect(config, "platform.unavailable")
    try:
        connection.execute("BEGIN")
        actor = authenticate_platform_session_in_transaction(
            connection, config, session_token=session_token, touch=False
        )
        _require_platform(actor, "platform.console.assistant.query")
        rows = _platform_source_rows(connection)
        (
            query_type,
            outcome,
            answer_code,
            claims,
            citations,
            sources,
            redaction_count,
        ) = _assistant_result(
            question,
            rows=rows,
            permissions=frozenset(actor.permissions),
            requirements=_PLATFORM_SOURCE_PERMISSIONS,
            citations=_PLATFORM_CITATIONS,
        )
        query_id = _id("paq_")
        fingerprint = _platform_fingerprint(actor)
        stamp = _db_time(_now())
        connection.execute(
            """INSERT INTO platform_assistant_query_projections(
                 platform_assistant_query_projection_id,operator_id,platform_session_id,
                 operator_revision,query_type,outcome,source_types_json,claim_count,
                 source_count,redaction_count,latency_bucket,permission_revision,
                 permission_fingerprint,execution_mode,failure_code,request_id,trace_id,
                 created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'deterministic_read_model_v1',?,?,?,?)""",
            (
                query_id,
                actor.operator_id,
                actor.platform_session_id,
                actor.operator_revision,
                query_type,
                outcome,
                _json([source.source_type for source in sources]),
                len(claims),
                len(sources),
                redaction_count,
                _latency_bucket(started_ns),
                actor.operator_revision,
                fingerprint,
                None if outcome in {"answered", "partial"} else "query_unsupported",
                request_id,
                trace_id,
                stamp,
            ),
        )
        result = AssistantQueryResponse(
            query_id=query_id,
            domain="platform",
            query_type=query_type,
            outcome=outcome,
            answer_code=answer_code,
            claims=claims,
            citations=citations,
            sources=sources,
            navigation_epoch=max(1, actor.operator_revision),
            permission_revision=actor.operator_revision,
            permission_fingerprint=fingerprint,
            request_id=request_id,
            trace_id=trace_id,
        )
        _write_read_audit(
            connection,
            config,
            actor_domain="platform",
            actor_id=actor.operator_id,
            owner=("platform",),
            action="platform.assistant.query",
            permission_revision=actor.operator_revision,
            fingerprint=fingerprint,
            watermark=f"query:{query_id}",
            outcome=_assistant_audit_outcome(outcome),
            request_id=request_id,
            trace_id=trace_id,
        )
        connection.commit()
        return result
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, DbError, ValueError, TypeError):
        _rollback(connection)
        raise ApiError("platform.unavailable") from None
    finally:
        connection.close()


def query_workspace_assistant(
    config: AppConfig,
    *,
    session_token: str,
    question: str,
    request_id: str,
    trace_id: str,
) -> AssistantQueryResponse:
    started_ns = time.perf_counter_ns()
    connection = _connect(config, "workspace_session.unavailable")
    try:
        connection.execute("BEGIN")
        actor = authenticate_workspace_session_in_transaction(
            connection, config, session_token=session_token, touch=False
        )
        _require_workspace(actor, "workspace.console.assistant.query")
        rows = _workspace_source_rows(connection, actor.workspace_id, actor.tenant_id)
        (
            query_type,
            outcome,
            answer_code,
            claims,
            citations,
            sources,
            redaction_count,
        ) = _assistant_result(
            question,
            rows=rows,
            permissions=frozenset(actor.permission_codes),
            requirements=_WORKSPACE_SOURCE_PERMISSIONS,
            citations=_WORKSPACE_CITATIONS,
        )
        query_id = _id("waq_")
        stamp = _db_time(_now())
        connection.execute(
            """INSERT INTO workspace_assistant_query_projections(
                 workspace_assistant_query_projection_id,workspace_id,tenant_id,account_id,
                 workspace_session_id,membership_revision,query_type,outcome,
                 source_types_json,claim_count,source_count,redaction_count,latency_bucket,
                 permission_revision,permission_fingerprint,execution_mode,failure_code,
                 request_id,trace_id,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'deterministic_read_model_v1',?,?,?,?)""",
            (
                query_id,
                actor.workspace_id,
                actor.tenant_id,
                actor.account_id,
                actor.workspace_session_id,
                actor.permission_revision,
                query_type,
                outcome,
                _json([source.source_type for source in sources]),
                len(claims),
                len(sources),
                redaction_count,
                _latency_bucket(started_ns),
                actor.permission_revision,
                actor.authorization_fingerprint,
                None if outcome in {"answered", "partial"} else "query_unsupported",
                request_id,
                trace_id,
                stamp,
            ),
        )
        result = AssistantQueryResponse(
            query_id=query_id,
            domain="workspace",
            query_type=query_type,
            outcome=outcome,
            answer_code=answer_code,
            claims=claims,
            citations=citations,
            sources=sources,
            navigation_epoch=max(1, actor.permission_revision),
            permission_revision=actor.permission_revision,
            permission_fingerprint=actor.authorization_fingerprint,
            request_id=request_id,
            trace_id=trace_id,
        )
        _write_read_audit(
            connection,
            config,
            actor_domain="workspace",
            actor_id=actor.account_id,
            owner=(actor.workspace_id, actor.tenant_id),
            action="workspace.assistant.query",
            permission_revision=actor.permission_revision,
            fingerprint=actor.authorization_fingerprint,
            watermark=f"query:{query_id}",
            outcome=_assistant_audit_outcome(outcome),
            request_id=request_id,
            trace_id=trace_id,
        )
        connection.commit()
        return result
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, DbError, ValueError, TypeError):
        _rollback(connection)
        raise ApiError("workspace_session.unavailable") from None
    finally:
        connection.close()


def list_platform_approvals(
    config: AppConfig,
    *,
    session_token: str,
    status_filter: str | None,
    risk_filter: str | None,
    source_domain: str | None,
    cursor: str | None,
    limit: int,
    request_id: str,
    trace_id: str,
) -> ApprovalQueueResponse:
    _validate_approval_query(
        status_filter,
        risk_filter,
        source_domain,
        limit,
        domains=frozenset(
            (
                "channel",
                "customer_success",
                "growth",
                "branding",
                "provider_engineering",
                "commercial_accounting",
                "security",
                "release",
            )
        ),
        error_code="platform.request_invalid",
    )
    connection = _connect(config, "platform.unavailable")
    try:
        connection.execute("BEGIN")
        actor = authenticate_platform_session_in_transaction(
            connection, config, session_token=session_token, touch=False
        )
        _require_platform(actor, "platform.console.approval.read")
        fingerprint = _platform_fingerprint(actor)
        filters = _approval_filters(status_filter, risk_filter, source_domain)
        position = _decode_approval_cursor(
            config,
            cursor,
            domain="platform",
            session_id=actor.platform_session_id,
            permission_revision=actor.operator_revision,
            fingerprint=fingerprint,
            owner=("platform",),
            filters=filters,
            error_code="platform.request_invalid",
        )
        rows = _select_approval_rows(
            connection,
            table="platform_domain_approval_queue_items",
            id_column="platform_approval_projection_id",
            owner_clause="",
            owner_values=(),
            filters=filters,
            position=position,
            limit=limit,
        )
        visible = rows[:limit]
        items = _approval_items(
            visible,
            id_column="platform_approval_projection_id",
            permissions=frozenset(actor.permissions),
            actor_pseudonym=_pseudonym(config, "platform", actor.operator_id),
        )
        next_cursor = None
        if len(rows) > limit:
            last = visible[-1]
            next_cursor = _encode_approval_cursor(
                config,
                domain="platform",
                session_id=actor.platform_session_id,
                permission_revision=actor.operator_revision,
                fingerprint=fingerprint,
                owner=("platform",),
                filters=filters,
                deadline_at=last["deadline_at"],
                projection_id=last["platform_approval_projection_id"],
            )
        watermark = _platform_approval_watermark(connection)
        result = ApprovalQueueResponse(
            domain="platform",
            items=items,
            next_cursor=next_cursor,
            navigation_epoch=max(1, actor.operator_revision),
            permission_revision=actor.operator_revision,
            permission_fingerprint=fingerprint,
            request_id=request_id,
            trace_id=trace_id,
        )
        _write_read_audit(
            connection,
            config,
            actor_domain="platform",
            actor_id=actor.operator_id,
            owner=("platform",),
            action="platform.approvals.read",
            permission_revision=actor.operator_revision,
            fingerprint=fingerprint,
            watermark=watermark,
            outcome="success" if items else "empty",
            request_id=request_id,
            trace_id=trace_id,
        )
        connection.commit()
        return result
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, DbError, ValueError, TypeError):
        _rollback(connection)
        raise ApiError("platform.unavailable") from None
    finally:
        connection.close()


def list_workspace_approvals(
    config: AppConfig,
    *,
    session_token: str,
    status_filter: str | None,
    risk_filter: str | None,
    source_domain: str | None,
    cursor: str | None,
    limit: int,
    request_id: str,
    trace_id: str,
) -> ApprovalQueueResponse:
    _validate_approval_query(
        status_filter,
        risk_filter,
        source_domain,
        limit,
        domains=frozenset(
            ("budget", "session_risk", "billing", "privacy", "compliance", "security")
        ),
        error_code="workspace_session.request_invalid",
    )
    connection = _connect(config, "workspace_session.unavailable")
    try:
        connection.execute("BEGIN")
        actor = authenticate_workspace_session_in_transaction(
            connection, config, session_token=session_token, touch=False
        )
        _require_workspace(actor, "workspace.console.approval.read")
        filters = _approval_filters(status_filter, risk_filter, source_domain)
        owner = (actor.workspace_id, actor.tenant_id)
        position = _decode_approval_cursor(
            config,
            cursor,
            domain="workspace",
            session_id=actor.workspace_session_id,
            permission_revision=actor.permission_revision,
            fingerprint=actor.authorization_fingerprint,
            owner=owner,
            filters=filters,
            error_code="workspace_session.request_invalid",
        )
        rows = _select_approval_rows(
            connection,
            table="workspace_domain_approval_queue_items",
            id_column="workspace_approval_projection_id",
            owner_clause="workspace_id=? AND tenant_id=?",
            owner_values=owner,
            filters=filters,
            position=position,
            limit=limit,
        )
        visible = rows[:limit]
        items = _approval_items(
            visible,
            id_column="workspace_approval_projection_id",
            permissions=frozenset(actor.permission_codes),
            actor_pseudonym=_pseudonym(config, "workspace", actor.account_id),
        )
        next_cursor = None
        if len(rows) > limit:
            last = visible[-1]
            next_cursor = _encode_approval_cursor(
                config,
                domain="workspace",
                session_id=actor.workspace_session_id,
                permission_revision=actor.permission_revision,
                fingerprint=actor.authorization_fingerprint,
                owner=owner,
                filters=filters,
                deadline_at=last["deadline_at"],
                projection_id=last["workspace_approval_projection_id"],
            )
        watermark = _workspace_approval_watermark(
            connection, actor.workspace_id, actor.tenant_id
        )
        result = ApprovalQueueResponse(
            domain="workspace",
            items=items,
            next_cursor=next_cursor,
            navigation_epoch=max(1, actor.permission_revision),
            permission_revision=actor.permission_revision,
            permission_fingerprint=actor.authorization_fingerprint,
            request_id=request_id,
            trace_id=trace_id,
        )
        _write_read_audit(
            connection,
            config,
            actor_domain="workspace",
            actor_id=actor.account_id,
            owner=owner,
            action="workspace.approvals.read",
            permission_revision=actor.permission_revision,
            fingerprint=actor.authorization_fingerprint,
            watermark=watermark,
            outcome="success" if items else "empty",
            request_id=request_id,
            trace_id=trace_id,
        )
        connection.commit()
        return result
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, DbError, ValueError, TypeError):
        _rollback(connection)
        raise ApiError("workspace_session.unavailable") from None
    finally:
        connection.close()


def get_account_preferences(
    config: AppConfig,
    *,
    session_token: str,
    request_id: str,
    trace_id: str,
) -> AccountPreferenceResponse:
    connection = _connect(config, "identity.unavailable")
    try:
        connection.execute("BEGIN")
        identity, _ = authenticate_global_session_in_transaction(
            connection, config, session_token=session_token, touch=False
        )
        preference = _ensure_account_preference(connection, identity.account_id)
        fingerprint = _identity_fingerprint(identity)
        result = _preference_response(preference, request_id, trace_id)
        _write_read_audit(
            connection,
            config,
            actor_domain="identity",
            actor_id=identity.account_id,
            owner=(identity.account_id,),
            action="identity.preferences.read",
            permission_revision=identity.current_session.revoke_generation,
            fingerprint=fingerprint,
            watermark=f"revision:{result.revision}",
            outcome="success",
            request_id=request_id,
            trace_id=trace_id,
        )
        connection.commit()
        return result
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, DbError, ValueError, TypeError):
        _rollback(connection)
        raise ApiError("identity.unavailable") from None
    finally:
        connection.close()


def change_account_preferences(
    config: AppConfig,
    *,
    session_token: str,
    csrf_token: str,
    expected_revision: int,
    idempotency_key: str,
    patch: AccountPreferencePatch,
    request_id: str,
    trace_id: str,
) -> AccountPreferenceCommandResponse:
    connection = _connect(config, "identity.unavailable")
    try:
        connection.execute("BEGIN IMMEDIATE")
        identity, now = authenticate_global_session_in_transaction(
            connection,
            config,
            session_token=session_token,
            csrf_token=csrf_token,
            touch=False,
        )
        preference = _ensure_account_preference(connection, identity.account_id)
        request_sha256 = _sha(
            {"expected_revision": expected_revision, "locale": patch.locale}
        )
        idempotency_hmac = _idempotency_hmac(config, idempotency_key)
        replay = connection.execute(
            """SELECT * FROM account_preference_commands
               WHERE account_id=? AND idempotency_hmac=?""",
            (identity.account_id, idempotency_hmac),
        ).fetchone()
        if replay is not None:
            if not hmac.compare_digest(replay["request_sha256"], request_sha256):
                raise ApiError("identity.idempotency_conflict")
            result = _preference_command_replay(
                preference["account_preference_id"], replay, request_id, trace_id
            )
            connection.commit()
            return result
        if type(expected_revision) is not int or expected_revision < 1:
            raise ApiError("identity.revision_required")
        if int(preference["revision"]) != expected_revision:
            _write_read_audit(
                connection,
                config,
                actor_domain="identity",
                actor_id=identity.account_id,
                owner=(identity.account_id,),
                action="identity.preferences.change",
                permission_revision=identity.current_session.revoke_generation,
                fingerprint=_identity_fingerprint(identity),
                watermark=f"revision:{preference['revision']}",
                outcome="conflict",
                request_id=request_id,
                trace_id=trace_id,
            )
            connection.commit()
            raise ApiError("identity.revision_conflict")
        old_locale = str(preference["locale"])
        changed = old_locale != patch.locale
        result_revision = expected_revision + 1 if changed else expected_revision
        stamp = _db_time(now)
        command_id = _id("apc_")
        if changed:
            updated = connection.execute(
                """UPDATE account_preferences
                   SET locale=?,revision=?,source='identity',confirmation_state='confirmed',
                     updated_at=?
                   WHERE account_id=? AND revision=? AND status='active'""",
                (
                    patch.locale,
                    result_revision,
                    stamp,
                    identity.account_id,
                    expected_revision,
                ),
            ).rowcount
            if updated != 1:
                raise ApiError("identity.revision_conflict")
        connection.execute(
            """INSERT INTO account_preference_commands(
                 account_preference_command_id,account_id,global_session_id,action,
                 idempotency_hmac,request_sha256,expected_revision,old_locale,new_locale,
                 result_revision,changed,request_id,trace_id,created_at)
               VALUES(?,?,?,'change_locale',?,?,?,?,?,?,?,?,?,?)""",
            (
                command_id,
                identity.account_id,
                identity.session_id,
                idempotency_hmac,
                request_sha256,
                expected_revision,
                old_locale,
                patch.locale,
                result_revision,
                int(changed),
                request_id,
                trace_id,
                stamp,
            ),
        )
        if changed:
            event_id = _id("ape_")
            connection.execute(
                """INSERT INTO account_preference_events(
                     account_preference_event_id,account_id,account_preference_command_id,
                     event_type,preference_id,revision,old_locale,new_locale,created_at)
                   VALUES(?,?,?,'account_preference.changed',?,?,?,?,?)""",
                (
                    event_id,
                    identity.account_id,
                    command_id,
                    preference["account_preference_id"],
                    result_revision,
                    old_locale,
                    patch.locale,
                    stamp,
                ),
            )
            connection.execute(
                """INSERT INTO account_preference_event_outbox(
                     account_preference_event_outbox_id,account_preference_event_id,status,
                     attempt_count,next_attempt_at,lease_owner_id,lease_generation,
                     lease_token_hmac,lease_expires_at,dispatched_at,created_at,updated_at)
                   VALUES(?,?,'pending',0,?,NULL,0,NULL,NULL,NULL,?,?)""",
                (_id("apo_"), event_id, stamp, stamp, stamp),
            )
        current = connection.execute(
            "SELECT * FROM account_preferences WHERE account_id=?",
            (identity.account_id,),
        ).fetchone()
        if current is None:
            raise ApiError("identity.unavailable")
        _write_read_audit(
            connection,
            config,
            actor_domain="identity",
            actor_id=identity.account_id,
            owner=(identity.account_id,),
            action="identity.preferences.change",
            permission_revision=identity.current_session.revoke_generation,
            fingerprint=_identity_fingerprint(identity),
            watermark=f"revision:{result_revision}",
            outcome="success",
            request_id=request_id,
            trace_id=trace_id,
        )
        connection.commit()
        return AccountPreferenceCommandResponse(
            **_preference_response(current, request_id, trace_id).model_dump(),
            changed=changed,
            idempotent_replay=False,
        )
    except ApiError:
        _rollback(connection)
        raise
    except sqlite3.IntegrityError:
        _rollback(connection)
        raise ApiError("identity.idempotency_conflict") from None
    except (sqlite3.Error, DbError, ValueError, TypeError):
        _rollback(connection)
        raise ApiError("identity.unavailable") from None
    finally:
        connection.close()


def get_workspace_deployment_summary(
    config: AppConfig,
    *,
    session_token: str,
    request_id: str,
    trace_id: str,
) -> DeploymentSummaryResponse:
    connection = _connect(config, "workspace_session.unavailable")
    try:
        connection.execute("BEGIN")
        actor = authenticate_workspace_session_in_transaction(
            connection, config, session_token=session_token, touch=False
        )
        permissions = frozenset(actor.permission_codes)
        operator_scope = "workspace.deployment_diagnostic.read" in permissions
        audit_scope = "workspace.deployment_diagnostic.audit_read" in permissions
        if not operator_scope and not audit_scope:
            raise ApiError("workspace_session.permission_denied")
        rows = connection.execute(
            """SELECT version.*,head.head_revision
               FROM workspace_deployment_diagnostic_heads head
               JOIN workspace_deployment_diagnostic_versions version
                 ON version.workspace_id=head.workspace_id
                AND version.tenant_id=head.tenant_id
                AND version.edge_deployment_id=head.edge_deployment_id
                AND version.deployment_diagnostic_projection_id=
                    head.deployment_diagnostic_projection_id
                AND version.projection_revision=head.projection_revision
               WHERE head.workspace_id=? AND head.tenant_id=?
               ORDER BY version.observed_at DESC,version.edge_deployment_id""",
            (actor.workspace_id, actor.tenant_id),
        ).fetchall()
        scope = "operator" if operator_scope else "audit"
        items = tuple(_deployment_item(config, row, scope=scope) for row in rows)
        generation = max((int(row["head_revision"]) for row in rows), default=0)
        result = DeploymentSummaryResponse(
            scope=scope,
            items=items,
            projection_generation=generation,
            navigation_epoch=max(1, actor.permission_revision),
            permission_revision=actor.permission_revision,
            permission_fingerprint=actor.authorization_fingerprint,
            request_id=request_id,
            trace_id=trace_id,
        )
        _write_read_audit(
            connection,
            config,
            actor_domain="workspace",
            actor_id=actor.account_id,
            owner=(actor.workspace_id, actor.tenant_id),
            action="workspace.deployment_summary.read",
            permission_revision=actor.permission_revision,
            fingerprint=actor.authorization_fingerprint,
            watermark=f"generation:{generation}",
            outcome="success" if items else "empty",
            request_id=request_id,
            trace_id=trace_id,
        )
        connection.commit()
        return result
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, DbError, ValueError, TypeError):
        _rollback(connection)
        raise ApiError("workspace_session.unavailable") from None
    finally:
        connection.close()


def _sections(
    rows: list[sqlite3.Row],
    *,
    permissions: frozenset[str],
    requirements: dict[str, str],
) -> tuple[ConsoleSection, ...]:
    grouped: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for row in rows:
        source_code = str(row["source_code"])
        required = requirements.get(source_code)
        if required is not None and required in permissions:
            grouped[source_code].append(row)
    result = []
    for source_code in sorted(grouped):
        source_rows = grouped[source_code]
        first = source_rows[0]
        if any(
            row["freshness"] != first["freshness"]
            or row["source_revision"] != first["source_revision"]
            or row["source_watermark"] != first["source_watermark"]
            for row in source_rows
        ):
            raise ValueError("mixed source snapshot")
        result.append(
            ConsoleSection(
                source_code=source_code,
                freshness=first["freshness"],
                source_revision=int(first["source_revision"]),
                source_watermark=first["source_watermark"],
                metrics=tuple(
                    ConsoleMetric(
                        metric_code=row["metric_code"],
                        value=row["metric_value"],
                        state_code=row["state_code"],
                        action_code=row["action_code"],
                        reason_code=row["reason_code"],
                    )
                    for row in source_rows
                ),
            )
        )
    return tuple(result)


def _platform_source_rows(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    head = connection.execute(
        """SELECT platform_dashboard_projection_id
           FROM platform_dashboard_projection_heads WHERE head_key='platform'"""
    ).fetchone()
    if head is None:
        return []
    return connection.execute(
        """SELECT * FROM platform_dashboard_projection_sources
           WHERE platform_dashboard_projection_id=? ORDER BY source_code,metric_code""",
        (head["platform_dashboard_projection_id"],),
    ).fetchall()


def _workspace_source_rows(
    connection: sqlite3.Connection,
    workspace_id: str,
    tenant_id: str,
) -> list[sqlite3.Row]:
    head = connection.execute(
        """SELECT workspace_dashboard_projection_id
           FROM workspace_dashboard_projection_heads
           WHERE workspace_id=? AND tenant_id=?""",
        (workspace_id, tenant_id),
    ).fetchone()
    if head is None:
        return []
    return connection.execute(
        """SELECT * FROM workspace_dashboard_projection_sources
           WHERE workspace_id=? AND tenant_id=?
             AND workspace_dashboard_projection_id=?
           ORDER BY source_code,metric_code""",
        (workspace_id, tenant_id, head["workspace_dashboard_projection_id"]),
    ).fetchall()


def _assistant_result(
    question: str,
    *,
    rows: list[sqlite3.Row],
    permissions: frozenset[str],
    requirements: dict[str, str],
    citations: dict[str, tuple[str, str]],
) -> tuple[
    str,
    str,
    str,
    tuple[AssistantClaim, ...],
    tuple[AssistantCitation, ...],
    tuple[AssistantSourceStatus, ...],
    int,
]:
    query_type = _classify_query(question)
    requested = frozenset(
        source for source in _QUERY_SOURCES[query_type] if source in requirements
    )
    rows_by_source: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for row in rows:
        rows_by_source[str(row["source_code"])].append(row)
    visible_sources = tuple(
        source
        for source in sorted(requested)
        if requirements[source] in permissions
    )
    redaction_count = len(requested) - len(visible_sources)
    claims: list[AssistantClaim] = []
    response_citations: list[AssistantCitation] = []
    source_statuses: list[AssistantSourceStatus] = []
    for source in visible_sources:
        source_rows = rows_by_source.get(source, [])
        status = _assistant_source_status(source_rows)
        source_statuses.append(AssistantSourceStatus(source_type=source, status=status))
        for row in source_rows:
            claims.append(
                AssistantClaim(
                    claim_code=str(row["metric_code"]),
                    value=row["metric_value"],
                    state_code=row["state_code"],
                )
            )
        if source_rows:
            label_code, navigation_id = citations[source]
            response_citations.append(
                AssistantCitation(
                    source_type=source,
                    label_code=label_code,
                    navigation_id=navigation_id,
                )
            )
    if query_type == "unknown" or not visible_sources:
        outcome = "unsupported"
        answer_code = "assistant.answer.unsupported"
    elif all(source.status == "ready" for source in source_statuses):
        outcome = "answered"
        answer_code = "assistant.answer.ready"
    else:
        outcome = "partial"
        answer_code = "assistant.answer.partial"
    return (
        query_type,
        outcome,
        answer_code,
        tuple(claims[:100]),
        tuple(response_citations[:50]),
        tuple(source_statuses[:50]),
        min(redaction_count, 100),
    )


def _classify_query(question: str) -> str:
    if type(question) is not str or not question.strip() or len(question) > 2000:
        raise ValueError("invalid question")
    normalized = question.casefold()
    for query_type, keywords in _QUERY_KEYWORDS.items():
        if any(keyword in normalized for keyword in keywords):
            return query_type
    return "unknown"


def _assistant_source_status(rows: list[sqlite3.Row]) -> str:
    if not rows:
        return "unavailable"
    freshness = {str(row["freshness"]) for row in rows}
    if freshness == {"fresh"}:
        return "ready"
    if "unavailable" in freshness:
        return "unavailable"
    return "stale"


def _assistant_audit_outcome(outcome: str) -> str:
    return {"answered": "success", "partial": "partial", "unsupported": "empty"}[
        outcome
    ]


def _validate_approval_query(
    status_filter: str | None,
    risk_filter: str | None,
    source_domain: str | None,
    limit: int,
    *,
    domains: frozenset[str],
    error_code: str,
) -> None:
    if (
        (status_filter is not None and status_filter not in _APPROVAL_FILTERS["status"])
        or (risk_filter is not None and risk_filter not in _APPROVAL_FILTERS["risk"])
        or (source_domain is not None and source_domain not in domains)
        or type(limit) is not int
        or not 1 <= limit <= 100
    ):
        raise ApiError(error_code)


def _approval_filters(
    status_filter: str | None,
    risk_filter: str | None,
    source_domain: str | None,
) -> tuple[str | None, str | None, str | None]:
    return status_filter, risk_filter, source_domain


def _select_approval_rows(
    connection: sqlite3.Connection,
    *,
    table: str,
    id_column: str,
    owner_clause: str,
    owner_values: tuple[str, ...],
    filters: tuple[str | None, str | None, str | None],
    position: tuple[str, str] | None,
    limit: int,
) -> list[sqlite3.Row]:
    allowed = {
        ("platform_domain_approval_queue_items", "platform_approval_projection_id"),
        ("workspace_domain_approval_queue_items", "workspace_approval_projection_id"),
    }
    if (table, id_column) not in allowed:
        raise ValueError("invalid approval source")
    clauses = [owner_clause] if owner_clause else []
    values: list[object] = list(owner_values)
    for column, value in zip(("status", "risk_band", "source_domain"), filters, strict=True):
        if value is not None:
            clauses.append(f"{column}=?")
            values.append(value)
    if position is not None:
        clauses.append(f"(deadline_at>? OR(deadline_at=? AND {id_column}>?))")
        values.extend((position[0], position[0], position[1]))
    where = " AND ".join(clauses) if clauses else "1=1"
    values.append(limit + 1)
    return connection.execute(
        f"""SELECT * FROM {table} WHERE {where}
            ORDER BY deadline_at,{id_column} LIMIT ?""",
        tuple(values),
    ).fetchall()


def _approval_items(
    rows: list[sqlite3.Row],
    *,
    id_column: str,
    permissions: frozenset[str],
    actor_pseudonym: str,
) -> tuple[ApprovalQueueItem, ...]:
    return tuple(
        ApprovalQueueItem(
            projection_id=row[id_column],
            source_domain=row["source_domain"],
            source_type=row["source_type"],
            source_revision=int(row["source_revision"]),
            status=row["status"],
            risk_band=row["risk_band"],
            summary_code=row["summary_code"],
            decision_contract_code=row["decision_contract_code"],
            deadline_at=_parse_time(row["deadline_at"]),
            can_open=(
                row["required_permission_code"] in permissions
                and row["applicant_pseudonym"] != actor_pseudonym
                and row["status"] == "pending"
            ),
        )
        for row in rows
    )


def _encode_approval_cursor(
    config: AppConfig,
    *,
    domain: str,
    session_id: str,
    permission_revision: int,
    fingerprint: str,
    owner: tuple[str, ...],
    filters: tuple[str | None, str | None, str | None],
    deadline_at: str,
    projection_id: str,
) -> str:
    payload = _json(
        {
            "d": domain,
            "e": int(_now().timestamp()) + _CURSOR_TTL_SECONDS,
            "f": list(filters),
            "h": fingerprint,
            "o": list(owner),
            "p": [deadline_at, projection_id],
            "r": permission_revision,
            "s": session_id,
            "v": 1,
        }
    ).encode("ascii")
    signature = hmac.new(_cursor_secret(config, domain), payload, hashlib.sha256).digest()
    return f"{_b64encode(payload)}.{_b64encode(signature)}"


def _decode_approval_cursor(
    config: AppConfig,
    cursor: str | None,
    *,
    domain: str,
    session_id: str,
    permission_revision: int,
    fingerprint: str,
    owner: tuple[str, ...],
    filters: tuple[str | None, str | None, str | None],
    error_code: str,
) -> tuple[str, str] | None:
    if cursor is None:
        return None
    try:
        if type(cursor) is not str or not 43 <= len(cursor) <= 512:
            raise ValueError
        encoded_payload, encoded_signature = cursor.split(".", 1)
        payload = _b64decode(encoded_payload)
        signature = _b64decode(encoded_signature)
        expected = hmac.new(
            _cursor_secret(config, domain), payload, hashlib.sha256
        ).digest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError
        parsed = json.loads(payload.decode("ascii"))
        expected_fields = {"d", "e", "f", "h", "o", "p", "r", "s", "v"}
        if type(parsed) is not dict or set(parsed) != expected_fields:
            raise ValueError
        canonical = _json(parsed).encode("ascii")
        if (
            canonical != payload
            or parsed["v"] != 1
            or parsed["d"] != domain
            or parsed["s"] != session_id
            or parsed["r"] != permission_revision
            or parsed["h"] != fingerprint
            or parsed["o"] != list(owner)
            or parsed["f"] != list(filters)
            or type(parsed["e"]) is not int
            or parsed["e"] < int(_now().timestamp())
            or type(parsed["p"]) is not list
            or len(parsed["p"]) != 2
            or not all(type(value) is str for value in parsed["p"])
        ):
            raise ValueError
        _parse_time(parsed["p"][0])
        return parsed["p"][0], parsed["p"][1]
    except (ValueError, TypeError, KeyError, UnicodeError, json.JSONDecodeError):
        raise ApiError(error_code) from None


def _cursor_secret(config: AppConfig, domain: str) -> bytes:
    secret = (
        config.platform_session_secret
        if domain == "platform"
        else config.workspace_session_secret
    )
    return f"prd145/{domain}/approval-cursor/v1\x00{secret}".encode()


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    return base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)


def _platform_approval_watermark(connection: sqlite3.Connection) -> str:
    row = connection.execute(
        """SELECT coalesce(max(generation),0),coalesce(max(source_revision),0)
           FROM platform_domain_approval_source_cursors"""
    ).fetchone()
    return f"generation:{int(row[0])}:revision:{int(row[1])}"


def _workspace_approval_watermark(
    connection: sqlite3.Connection, workspace_id: str, tenant_id: str
) -> str:
    row = connection.execute(
        """SELECT coalesce(max(generation),0),coalesce(max(source_revision),0)
           FROM workspace_domain_approval_source_cursors
           WHERE workspace_id=? AND tenant_id=?""",
        (workspace_id, tenant_id),
    ).fetchone()
    return f"generation:{int(row[0])}:revision:{int(row[1])}"


def _ensure_account_preference(
    connection: sqlite3.Connection, account_id: str
) -> sqlite3.Row:
    stamp = _db_time(_now())
    connection.execute(
        """INSERT OR IGNORE INTO account_preferences(
             account_preference_id,account_id,locale,status,revision,source,
             confirmation_state,created_at,updated_at)
           VALUES(?,?,'zh-CN','active',1,'identity','confirmed',?,?)""",
        (_stable_id("acp_", f"account-preference:{account_id}"), account_id, stamp, stamp),
    )
    row = connection.execute(
        "SELECT * FROM account_preferences WHERE account_id=? AND status='active'",
        (account_id,),
    ).fetchone()
    if row is None:
        raise ApiError("identity.unavailable")
    return row


def _preference_response(
    row: sqlite3.Row, request_id: str, trace_id: str
) -> AccountPreferenceResponse:
    return AccountPreferenceResponse(
        preference_id=row["account_preference_id"],
        locale=row["locale"],
        available_locales=("zh-CN", "en-US"),
        revision=int(row["revision"]),
        confirmation_state=row["confirmation_state"],
        updated_at=_parse_time(row["updated_at"]),
        request_id=request_id,
        trace_id=trace_id,
    )


def _preference_command_replay(
    preference_id: str,
    row: sqlite3.Row,
    request_id: str,
    trace_id: str,
) -> AccountPreferenceCommandResponse:
    return AccountPreferenceCommandResponse(
        preference_id=preference_id,
        locale=row["new_locale"],
        available_locales=("zh-CN", "en-US"),
        revision=int(row["result_revision"]),
        confirmation_state="confirmed",
        updated_at=_parse_time(row["created_at"]),
        request_id=request_id,
        trace_id=trace_id,
        changed=bool(row["changed"]),
        idempotent_replay=True,
    )


def _deployment_item(
    config: AppConfig, row: sqlite3.Row, *, scope: str
) -> DeploymentDiagnostic:
    operator_scope = scope == "operator"
    return DeploymentDiagnostic(
        deployment_ref=_pseudonym(
            config, "workspace-deployment", row["edge_deployment_id"]
        ),
        deployment_kind=row["deployment_kind"] if operator_scope else None,
        region_code=row["region_code"] if operator_scope else None,
        release_band=row["release_band"],
        compatibility_band=row["compatibility_band"],
        desired_generation=(int(row["desired_generation"]) if operator_scope else None),
        observed_generation=(
            int(row["observed_generation"]) if operator_scope else None
        ),
        health_state=row["health_state"],
        reason_code=row["reason_code"],
        action_code=row["action_code"] if operator_scope else None,
        evidence_digest=row["evidence_digest"],
        observed_at=_parse_time(row["observed_at"]),
    )


def _write_read_audit(
    connection: sqlite3.Connection,
    config: AppConfig,
    *,
    actor_domain: str,
    actor_id: str,
    owner: tuple[str, ...],
    action: str,
    permission_revision: int,
    fingerprint: str,
    watermark: str,
    outcome: str,
    request_id: str,
    trace_id: str,
) -> None:
    connection.execute(
        """INSERT INTO console_experience_read_audits(
             console_experience_read_audit_id,actor_domain,actor_pseudonym,owner_digest,
             action_code,permission_revision,permission_fingerprint,projection_watermark,
             outcome,request_id,trace_id,created_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            _id("cra_"),
            actor_domain,
            _pseudonym(config, actor_domain, actor_id),
            _pseudonym(config, f"{actor_domain}-owner", "\x1f".join(owner)),
            action,
            permission_revision,
            fingerprint,
            watermark,
            outcome,
            request_id,
            trace_id,
            _db_time(_now()),
        ),
    )


def _require_platform(actor: PlatformContext, permission: str) -> None:
    if permission not in actor.permissions:
        raise ApiError("platform.permission_denied")


def _require_workspace(actor: WorkspaceSessionContext, permission: str) -> None:
    if permission not in actor.permission_codes:
        raise ApiError("workspace_session.permission_denied")


def _platform_fingerprint(actor: PlatformContext) -> str:
    return _sha(
        {
            "operator_revision": actor.operator_revision,
            "permissions": sorted(actor.permissions),
            "roles": sorted(actor.roles),
        }
    )


def _identity_fingerprint(identity: object) -> str:
    return _sha(
        {
            "account_id": identity.account_id,
            "revoke_generation": identity.current_session.revoke_generation,
            "session_id": identity.session_id,
        }
    )


def _idempotency_hmac(config: AppConfig, value: str) -> str:
    return hmac.new(
        config.identity_session_secret.encode("utf-8"),
        b"prd145/account-preference/idempotency/v1\x00" + value.encode("ascii"),
        hashlib.sha256,
    ).hexdigest()


def _pseudonym(config: AppConfig, namespace: str, value: str) -> str:
    return hmac.new(
        config.workspace_session_secret.encode("utf-8"),
        f"prd145/{namespace}/pseudonym/v1\x00{value}".encode(),
        hashlib.sha256,
    ).hexdigest()


def _read_outcome(status: str, has_sections: bool) -> str:
    if status == "stale":
        return "stale"
    if status == "partial":
        return "partial"
    return "success" if has_sections else "empty"


def _latency_bucket(started_ns: int) -> str:
    milliseconds = (time.perf_counter_ns() - started_ns) / 1_000_000
    for boundary, label in (
        (50, "lt_50ms"),
        (150, "lt_150ms"),
        (300, "lt_300ms"),
        (1000, "lt_1000ms"),
    ):
        if milliseconds < boundary:
            return label
    return "gte_1000ms"


def _connect(config: AppConfig, error_code: str) -> sqlite3.Connection:
    try:
        return connect_database(config.database_url)
    except DbError:
        raise ApiError(error_code) from None


def _rollback(connection: sqlite3.Connection) -> None:
    if connection.in_transaction:
        connection.rollback()


def _id(prefix: str) -> str:
    return prefix + uuid4().hex


def _stable_id(prefix: str, seed: str) -> str:
    return prefix + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:32]


def _sha(value: object) -> str:
    return hashlib.sha256(_json(value).encode("ascii")).hexdigest()


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timezone required")
    return parsed.astimezone(UTC)


def _db_time(value: datetime) -> str:
    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


__all__ = [
    "change_account_preferences",
    "get_account_preferences",
    "get_platform_overview",
    "get_workspace_deployment_summary",
    "get_workspace_overview",
    "list_platform_approvals",
    "list_workspace_approvals",
    "query_platform_assistant",
    "query_workspace_assistant",
]
