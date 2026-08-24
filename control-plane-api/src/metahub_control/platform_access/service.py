"""Database-authoritative PlatformSession and controlled support services."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import re
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from metahub_shared.db.connection import connect_database
from metahub_shared.db.security import verify_password
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError

from metahub_control.identity.mfa_service import consume_active_totp_in_transaction
from metahub_control.identity.service import (
    authenticate_global_session_in_transaction,
    valid_opaque_value,
)
from metahub_control.workspace_access.service import (
    authenticate_workspace_session_in_transaction,
)

from .models import (
    BreakGlassReviewResponse,
    BreakGlassSessionResponse,
    PlatformAuditListResponse,
    PlatformAuditSummary,
    PlatformBootstrapResponse,
    PlatformNavigationItem,
    PlatformNavigationResponse,
    PlatformOperatorListResponse,
    PlatformOperatorResponse,
    PlatformOperatorSummary,
    PlatformSessionContext,
    PlatformSessionResponse,
    SupportDiagnosticsResponse,
    SupportGrantListResponse,
    SupportGrantResponse,
    SupportGrantSummary,
    SupportSessionResponse,
    SupportSessionSummary,
)

_OPERATOR_ID_RE = re.compile(r"^pto_[0-9a-f]{32}$")
_WORKSPACE_ID_RE = re.compile(r"^wsp_[0-9a-f]{32}$")
_GRANT_ID_RE = re.compile(r"^sgr_[0-9a-f]{32}$")
_BREAK_GLASS_ID_RE = re.compile(r"^bgs_[0-9a-f]{32}$")
_AUDIT_ID_RE = re.compile(r"^pau_[0-9a-f]{32}$")
_AUDIT_CURSOR_DOMAIN = b"prd095/platform/audit-cursor/v1"
_SECURITY_AUDIT_ACTIONS = (
    "break_glass.reviewed",
    "break_glass.used",
    "platform.bootstrap_completed",
    "platform.operator_created",
    "platform.operator_updated",
    "platform.session_issued",
    "support.access_approved",
    "support.access_requested",
    "support.access_revoked",
    "support.session_issued",
    "workspace.owner_notification_requested",
    "workspace.restored",
    "workspace.risk_tag_added",
    "workspace.risk_tag_removed",
    "workspace.suspended",
)
_ROLE_CODES = frozenset(
    {
        "platform_owner",
        "platform_ops",
        "platform_security",
        "platform_billing",
        "platform_support",
        "platform_verification",
        "platform_compliance",
        "platform_privacy",
        "release_manager",
        "platform_provider_engineer",
        "platform_channel",
        "platform_customer_success",
        "platform_growth",
        "platform_communications",
    }
)
_NAVIGATION = (
    (
        "platform.infrastructure.read",
        "infrastructure",
        "Infrastructure",
        "/platform.html#infrastructure",
    ),
    ("platform.gateway.read", "gateway", "Gateway", "/platform.html#gateway"),
    (
        "platform.job.read",
        "job-runtime",
        "Job runtime",
        "/platform.html#job-runtime",
    ),
    (
        "platform.feature.read",
        "feature-rollouts",
        "Feature rollouts",
        "/platform.html#feature-rollouts",
    ),
    (
        "platform.config_distribution.read",
        "config-distribution",
        "Config distribution",
        "/platform.html#config-distribution",
    ),
    (
        "platform.observability.read",
        "observability",
        "Observability",
        "/platform.html#observability",
    ),
    (
        "platform.risk.read",
        "abuse-risk",
        "Abuse risk",
        "/platform.html#abuse-risk",
    ),
    (
        "platform.dr.read",
        "disaster-recovery",
        "Disaster recovery",
        "/platform.html#disaster-recovery",
    ),
    ("platform.operator.manage", "operators", "Operators", "/platform.html#operators"),
    ("platform.workspace.directory.read", "workspaces", "Workspaces", "/platform.html#workspaces"),
    ("platform.support.request", "support", "Support", "/platform.html#support"),
    ("platform.audit.read", "audit", "Audit", "/platform.html#audit"),
    ("platform.billing.read", "billing", "Billing", "/platform.html#billing"),
    ("platform.verification.read", "verification", "Verification", "/platform.html#verification"),
    ("platform.compliance.read", "compliance", "Compliance", "/platform.html#compliance"),
    ("platform.privacy.read", "privacy", "Privacy", "/platform.html#privacy"),
    ("platform.release.read", "releases", "Releases", "/platform.html#releases"),
    ("platform.provider.read", "providers", "Provider engineering", "/platform.html#providers"),
    ("platform.channel.read", "channels", "Channels", "/platform.html#channels"),
    (
        "platform.customer_success.read",
        "customer-success",
        "Customer success",
        "/platform.html#customer-success",
    ),
    ("platform.growth.read", "growth", "Growth", "/platform.html#growth"),
    (
        "platform.notification_template.read",
        "notification-templates",
        "Notification templates",
        "/platform.html#notification-templates",
    ),
)


@dataclass(frozen=True, slots=True)
class PlatformContext:
    platform_session_id: str
    operator_id: str
    account_id: str
    global_session_id: str
    factor_id: str
    factor_revision: int
    operator_revision: int
    roles: tuple[str, ...]
    permissions: tuple[str, ...]
    expires_at: datetime


@dataclass(frozen=True, slots=True, repr=False)
class IssuedPlatformSession:
    response: PlatformSessionResponse
    session_token: str
    csrf_token: str


@dataclass(frozen=True, slots=True, repr=False)
class IssuedSupportSession:
    response: SupportSessionResponse | BreakGlassSessionResponse
    session_token: str


def authenticate_platform_session_context(
    config: AppConfig,
    session_token: str,
    *,
    csrf_token: str | None = None,
) -> PlatformContext:
    """Restore one Platform principal for domain routers before body parsing."""

    connection = _connect(config)
    try:
        connection.execute("BEGIN")
        context = _authenticate_platform(
            connection,
            config,
            session_token,
            csrf_token=csrf_token,
            touch=True,
        )
        connection.commit()
        return context
    except ApiError as exc:
        _finish_auth_error(connection, exc)
        raise
    except (sqlite3.Error, ValueError, TypeError):
        _rollback(connection)
        raise ApiError("platform.unavailable") from None
    finally:
        connection.close()


def authenticate_platform_session_in_transaction(
    connection: sqlite3.Connection,
    config: AppConfig,
    *,
    session_token: str,
    csrf_token: str | None = None,
    touch: bool = False,
) -> PlatformContext:
    """Authenticate a PlatformSession inside a caller-owned transaction."""

    return _authenticate_platform(
        connection,
        config,
        session_token,
        csrf_token=csrf_token,
        touch=touch,
    )


def bootstrap_platform_owner(
    config: AppConfig,
    *,
    global_session_token: str,
    global_csrf_token: str,
    current_password: str,
    totp_code: str,
    recovery_secret: str,
    reason_code: str,
    device_fingerprint: str,
    request_id: str,
    trace_id: str,
) -> PlatformBootstrapResponse:
    if not config.platform_bootstrap_enabled:
        raise ApiError("platform.bootstrap_disabled")
    if not hmac.compare_digest(recovery_secret, config.platform_bootstrap_recovery_secret):
        raise ApiError("platform.permission_denied")
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        identity, now = authenticate_global_session_in_transaction(
            connection,
            config,
            session_token=global_session_token,
            csrf_token=global_csrf_token,
        )
        _verify_current_password(connection, identity.account_id, current_password)
        factor_id, _factor_revision, _counter = consume_active_totp_in_transaction(
            connection,
            config,
            account_id=identity.account_id,
            code=totp_code,
            device_fingerprint=device_fingerprint,
            now=now,
        )
        state = connection.execute(
            "SELECT status FROM platform_bootstrap_state WHERE singleton_id=1"
        ).fetchone()
        if state is None:
            raise ApiError("platform.unavailable")
        if (
            state["status"] != "open"
            or connection.execute("SELECT 1 FROM platform_operators LIMIT 1").fetchone() is not None
        ):
            raise ApiError("platform.bootstrap_completed")
        operator_id = _id("pto_")
        connection.execute(
            """
            INSERT INTO platform_operators(
              operator_id,account_id,status,revision,created_by_operator_id,created_at,updated_at)
            VALUES(?,?,'active',1,NULL,?,?)
            """,
            (operator_id, identity.account_id, _db_time(now), _db_time(now)),
        )
        connection.execute(
            """
            INSERT INTO platform_operator_role_bindings(
              operator_id,role_code,status,granted_by_operator_id,granted_at,revoked_at)
            VALUES(?,'platform_owner','active',NULL,?,NULL)
            """,
            (operator_id, _db_time(now)),
        )
        updated = connection.execute(
            """
            UPDATE platform_bootstrap_state SET status='completed',operator_id=?,completed_at=?
            WHERE singleton_id=1 AND status='open'
            """,
            (operator_id, _db_time(now)),
        ).rowcount
        if updated != 1:
            raise ApiError("platform.bootstrap_completed")
        _audit(
            connection,
            actor_domain="global",
            actor_id=identity.account_id,
            action="platform.bootstrap_completed",
            object_type="platform_operator",
            object_id=operator_id,
            reason=reason_code,
            request_id=request_id,
            trace_id=trace_id,
            now=now,
            event_type="platform.operator_changed",
            after={"factor_id": factor_id, "revision": 1},
        )
        summary = _operator_summary(connection, operator_id)
        connection.commit()
        return PlatformBootstrapResponse(
            operator=summary,
            request_id=request_id,
            trace_id=trace_id,
        )
    except ApiError as exc:
        _finish_mfa_write_error(connection, exc)
        raise
    except (sqlite3.Error, ValueError, TypeError):
        _rollback(connection)
        raise ApiError("platform.unavailable") from None
    finally:
        connection.close()


def issue_platform_session(
    config: AppConfig,
    *,
    global_session_token: str,
    global_csrf_token: str,
    totp_code: str,
    device_fingerprint: str,
    request_id: str,
    trace_id: str,
) -> IssuedPlatformSession:
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        identity, now = authenticate_global_session_in_transaction(
            connection,
            config,
            session_token=global_session_token,
            csrf_token=global_csrf_token,
        )
        operator = connection.execute(
            "SELECT * FROM platform_operators WHERE account_id=? AND status='active'",
            (identity.account_id,),
        ).fetchone()
        if operator is None:
            raise ApiError("platform.permission_denied")
        factor_id, factor_revision, _counter = consume_active_totp_in_transaction(
            connection,
            config,
            account_id=identity.account_id,
            code=totp_code,
            device_fingerprint=device_fingerprint,
            now=now,
        )
        roles, permissions, fingerprint = _authority(connection, operator["operator_id"])
        if not roles:
            raise ApiError("platform.permission_denied")
        raw_token = secrets.token_urlsafe(48)
        raw_csrf = secrets.token_urlsafe(32)
        session_id = _id("pts_")
        expires = now + timedelta(seconds=config.platform_session_ttl_seconds)
        connection.execute(
            """
            UPDATE platform_sessions SET status='revoked',revoked_at=?,
              revoked_reason='rotated',updated_at=?
            WHERE operator_id=? AND status='active'
            """,
            (_db_time(now), _db_time(now), operator["operator_id"]),
        )
        connection.execute(
            """
            INSERT INTO platform_sessions(
              platform_session_id,operator_id,account_id,global_session_id,factor_id,
              token_hmac,csrf_hmac,status,operator_revision,factor_revision,role_fingerprint,
              last_seen_at,absolute_expires_at,idle_expires_at,revoked_at,revoked_reason,
              created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,'active',?,?,?,?,?,?,NULL,NULL,?,?)
            """,
            (
                session_id,
                operator["operator_id"],
                identity.account_id,
                identity.session_id,
                factor_id,
                _platform_hmac(config, "token", raw_token),
                _platform_hmac(config, "csrf", raw_csrf),
                operator["revision"],
                factor_revision,
                fingerprint,
                _db_time(now),
                _db_time(expires),
                _db_time(expires),
                _db_time(now),
                _db_time(now),
            ),
        )
        _audit(
            connection,
            actor_domain="platform",
            actor_id=operator["operator_id"],
            action="platform.session_issued",
            object_type="platform_session",
            object_id=session_id,
            reason="mfa_verified",
            request_id=request_id,
            trace_id=trace_id,
            now=now,
        )
        response = PlatformSessionResponse(
            context=PlatformSessionContext(
                operator=_operator_summary(connection, operator["operator_id"]),
                permissions=permissions,
                expires_at=expires,
            ),
            request_id=request_id,
            trace_id=trace_id,
        )
        connection.commit()
        return IssuedPlatformSession(
            response=response,
            session_token=raw_token,
            csrf_token=raw_csrf,
        )
    except ApiError as exc:
        _finish_mfa_write_error(connection, exc)
        raise
    except (sqlite3.Error, ValueError, TypeError):
        _rollback(connection)
        raise ApiError("platform.unavailable") from None
    finally:
        connection.close()


def get_platform_session(
    config: AppConfig, *, session_token: str, request_id: str, trace_id: str
) -> PlatformSessionResponse:
    connection = _connect(config)
    try:
        connection.execute("BEGIN")
        context = _authenticate_platform(connection, config, session_token, touch=True)
        response = PlatformSessionResponse(
            context=PlatformSessionContext(
                operator=_operator_summary(connection, context.operator_id),
                permissions=context.permissions,
                expires_at=context.expires_at,
            ),
            request_id=request_id,
            trace_id=trace_id,
        )
        connection.commit()
        return response
    except ApiError as exc:
        _finish_auth_error(connection, exc)
        raise
    except (sqlite3.Error, ValueError, TypeError):
        _rollback(connection)
        raise ApiError("platform.unavailable") from None
    finally:
        connection.close()


def list_platform_operators(
    config: AppConfig,
    *,
    session_token: str,
    limit: int,
    offset: int,
    request_id: str,
    trace_id: str,
) -> PlatformOperatorListResponse:
    connection = _connect(config)
    try:
        connection.execute("BEGIN")
        context = _authenticate_platform(connection, config, session_token, touch=True)
        _require_permission(context, "platform.operator.manage")
        ids = connection.execute(
            """
            SELECT operator_id FROM platform_operators
            ORDER BY created_at,operator_id LIMIT ? OFFSET ?
            """,
            (limit, offset),
        ).fetchall()
        total = int(connection.execute("SELECT COUNT(*) FROM platform_operators").fetchone()[0])
        response = PlatformOperatorListResponse(
            operators=tuple(_operator_summary(connection, row["operator_id"]) for row in ids),
            total=total,
            limit=limit,
            offset=offset,
            request_id=request_id,
            trace_id=trace_id,
        )
        connection.commit()
        return response
    except ApiError as exc:
        _finish_auth_error(connection, exc)
        raise
    except (sqlite3.Error, ValueError, TypeError):
        _rollback(connection)
        raise ApiError("platform.unavailable") from None
    finally:
        connection.close()


def create_platform_operator(
    config: AppConfig,
    *,
    session_token: str,
    csrf_token: str,
    account_id: str,
    roles: tuple[str, ...],
    request_id: str,
    trace_id: str,
) -> PlatformOperatorResponse:
    checked_roles = _checked_roles(roles)
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        actor = _authenticate_platform(
            connection, config, session_token, csrf_token=csrf_token, touch=True
        )
        _require_permission(actor, "platform.operator.manage")
        account = connection.execute(
            """
            SELECT account_id FROM global_accounts
            WHERE account_id=? AND status='active' AND login_enabled=1
              AND EXISTS(SELECT 1 FROM account_mfa_factors factor
                WHERE factor.account_id=global_accounts.account_id AND factor.status='active')
            """,
            (account_id,),
        ).fetchone()
        if account is None:
            raise ApiError("platform.operator_not_found")
        if (
            connection.execute(
                "SELECT 1 FROM platform_operators WHERE account_id=?", (account_id,)
            ).fetchone()
            is not None
        ):
            raise ApiError("platform.revision_conflict")
        now = _utcnow()
        operator_id = _id("pto_")
        connection.execute(
            """
            INSERT INTO platform_operators(
              operator_id,account_id,status,revision,created_by_operator_id,created_at,updated_at)
            VALUES(?,?,'active',1,?,?,?)
            """,
            (operator_id, account_id, actor.operator_id, _db_time(now), _db_time(now)),
        )
        for role in checked_roles:
            connection.execute(
                """
                INSERT INTO platform_operator_role_bindings(
                  operator_id,role_code,status,granted_by_operator_id,granted_at,revoked_at)
                VALUES(?,?,'active',?,?,NULL)
                """,
                (operator_id, role, actor.operator_id, _db_time(now)),
            )
        _audit(
            connection,
            actor_domain="platform",
            actor_id=actor.operator_id,
            action="platform.operator_created",
            object_type="platform_operator",
            object_id=operator_id,
            reason="owner_delegation",
            request_id=request_id,
            trace_id=trace_id,
            now=now,
            event_type="platform.operator_changed",
            after={"status": "active", "roles": checked_roles, "revision": 1},
        )
        response = PlatformOperatorResponse(
            operator=_operator_summary(connection, operator_id),
            request_id=request_id,
            trace_id=trace_id,
        )
        connection.commit()
        return response
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, ValueError, TypeError):
        _rollback(connection)
        raise ApiError("platform.unavailable") from None
    finally:
        connection.close()


def update_platform_operator(
    config: AppConfig,
    *,
    session_token: str,
    csrf_token: str,
    operator_id: str,
    expected_revision: int,
    status: str,
    roles: tuple[str, ...],
    request_id: str,
    trace_id: str,
) -> PlatformOperatorResponse:
    if _OPERATOR_ID_RE.fullmatch(operator_id) is None or status not in {"active", "disabled"}:
        raise ApiError("platform.request_invalid")
    checked_roles = _checked_roles(roles)
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        actor = _authenticate_platform(
            connection, config, session_token, csrf_token=csrf_token, touch=True
        )
        _require_permission(actor, "platform.operator.manage")
        target = connection.execute(
            "SELECT * FROM platform_operators WHERE operator_id=?", (operator_id,)
        ).fetchone()
        if target is None:
            raise ApiError("platform.operator_not_found")
        if int(target["revision"]) != expected_revision:
            raise ApiError("platform.revision_conflict")
        target_roles, _permissions, _fingerprint = _authority(connection, operator_id)
        removes_owner = "platform_owner" in target_roles and (
            status != "active" or "platform_owner" not in checked_roles
        )
        if removes_owner and _active_owner_count(connection) <= 1:
            raise ApiError("platform.last_owner_required")
        now = _utcnow()
        updated = connection.execute(
            """
            UPDATE platform_operators SET status=?,revision=revision+1,updated_at=?
            WHERE operator_id=? AND revision=?
            """,
            (status, _db_time(now), operator_id, expected_revision),
        ).rowcount
        if updated != 1:
            raise ApiError("platform.revision_conflict")
        for role in _ROLE_CODES:
            if role in checked_roles:
                connection.execute(
                    """
                    INSERT INTO platform_operator_role_bindings(
                      operator_id,role_code,status,granted_by_operator_id,granted_at,revoked_at)
                    VALUES(?,?,'active',?,?,NULL)
                    ON CONFLICT(operator_id,role_code) DO UPDATE SET
                      status='active',granted_by_operator_id=excluded.granted_by_operator_id,
                      granted_at=excluded.granted_at,revoked_at=NULL
                    """,
                    (operator_id, role, actor.operator_id, _db_time(now)),
                )
            else:
                connection.execute(
                    """
                    UPDATE platform_operator_role_bindings SET status='revoked',revoked_at=?
                    WHERE operator_id=? AND role_code=? AND status='active'
                    """,
                    (_db_time(now), operator_id, role),
                )
        _audit(
            connection,
            actor_domain="platform",
            actor_id=actor.operator_id,
            action="platform.operator_updated",
            object_type="platform_operator",
            object_id=operator_id,
            reason="owner_delegation",
            request_id=request_id,
            trace_id=trace_id,
            now=now,
            event_type="platform.operator_changed",
            before={
                "status": target["status"],
                "roles": target_roles,
                "revision": expected_revision,
            },
            after={"status": status, "roles": checked_roles, "revision": expected_revision + 1},
        )
        response = PlatformOperatorResponse(
            operator=_operator_summary(connection, operator_id),
            request_id=request_id,
            trace_id=trace_id,
        )
        connection.commit()
        return response
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, ValueError, TypeError):
        _rollback(connection)
        raise ApiError("platform.unavailable") from None
    finally:
        connection.close()


def get_platform_navigation(
    config: AppConfig, *, session_token: str, request_id: str, trace_id: str
) -> PlatformNavigationResponse:
    connection = _connect(config)
    try:
        connection.execute("BEGIN")
        context = _authenticate_platform(connection, config, session_token, touch=True)
        permission_set = set(context.permissions)
        items = tuple(
            PlatformNavigationItem(id=item_id, label=label, href=href)
            for permission, item_id, label, href in _NAVIGATION
            if permission in permission_set
            or (item_id == "billing" and "platform.catalog.read" in permission_set)
            or (
                item_id == "observability"
                and "platform.observability.security_read" in permission_set
            )
            or (item_id == "abuse-risk" and "platform.risk.support_read" in permission_set)
        )
        connection.commit()
        return PlatformNavigationResponse(items=items, request_id=request_id, trace_id=trace_id)
    except ApiError as exc:
        _finish_auth_error(connection, exc)
        raise
    except (sqlite3.Error, ValueError, TypeError):
        _rollback(connection)
        raise ApiError("platform.unavailable") from None
    finally:
        connection.close()


def create_support_grant(
    config: AppConfig,
    *,
    session_token: str,
    csrf_token: str,
    workspace_id: str,
    reason_code: str,
    duration_minutes: int,
    request_id: str,
    trace_id: str,
) -> SupportGrantResponse:
    if _WORKSPACE_ID_RE.fullmatch(workspace_id) is None or not 1 <= duration_minutes <= 30:
        raise ApiError("platform.request_invalid")
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        actor = _authenticate_platform(
            connection, config, session_token, csrf_token=csrf_token, touch=True
        )
        _require_permission(actor, "platform.support.request")
        workspace = connection.execute(
            """
            SELECT workspace.workspace_id,binding.tenant_id,workspace.revision,workspace.status
            FROM workspaces workspace JOIN workspace_tenant_bindings binding
              ON binding.workspace_id=workspace.workspace_id
            WHERE workspace.workspace_id=? AND binding.status='active'
            """,
            (workspace_id,),
        ).fetchone()
        if workspace is None or workspace["status"] != "active":
            raise ApiError("support.grant_not_found")
        now = _utcnow()
        grant_id = _id("sgr_")
        expires = now + timedelta(minutes=duration_minutes)
        connection.execute(
            """
            INSERT INTO support_grants(
              grant_id,requester_operator_id,workspace_id,tenant_id,scope_code,reason_code,
              status,operator_revision,workspace_revision,expires_at,revoked_at,created_at,updated_at)
            VALUES(?,?,?,?,'workspace.diagnostics.read',?,'pending_approvals',?,?,?,NULL,?,?)
            """,
            (
                grant_id,
                actor.operator_id,
                workspace_id,
                workspace["tenant_id"],
                reason_code,
                actor.operator_revision,
                workspace["revision"],
                _db_time(expires),
                _db_time(now),
                _db_time(now),
            ),
        )
        _audit(
            connection,
            actor_domain="platform",
            actor_id=actor.operator_id,
            action="support.access_requested",
            object_type="support_grant",
            object_id=grant_id,
            reason=reason_code,
            request_id=request_id,
            trace_id=trace_id,
            now=now,
            event_type="support.access_requested",
            after={"workspace_id": workspace_id, "expires_at": _db_time(expires)},
        )
        response = _grant_response(connection, grant_id, request_id, trace_id, now)
        connection.commit()
        return response
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, ValueError, TypeError):
        _rollback(connection)
        raise ApiError("platform.unavailable") from None
    finally:
        connection.close()


def list_support_grants(
    config: AppConfig,
    *,
    session_token: str,
    limit: int,
    offset: int,
    request_id: str,
    trace_id: str,
) -> SupportGrantListResponse:
    connection = _connect(config)
    try:
        connection.execute("BEGIN")
        actor = _authenticate_platform(connection, config, session_token, touch=True)
        can_review_all = "platform.support.approve" in actor.permissions
        if not can_review_all and "platform.support.request" not in actor.permissions:
            raise ApiError("platform.permission_denied")
        where = "" if can_review_all else "WHERE requester_operator_id=?"
        parameters: tuple[object, ...] = () if can_review_all else (actor.operator_id,)
        rows = connection.execute(
            f"""
            SELECT * FROM support_grants {where}
            ORDER BY created_at DESC,grant_id DESC LIMIT ? OFFSET ?
            """,
            (*parameters, limit, offset),
        ).fetchall()
        total = int(
            connection.execute(
                f"SELECT COUNT(*) FROM support_grants {where}", parameters
            ).fetchone()[0]
        )
        now = _utcnow()
        response = SupportGrantListResponse(
            grants=tuple(_grant_summary(connection, row, now) for row in rows),
            total=total,
            limit=limit,
            offset=offset,
            request_id=request_id,
            trace_id=trace_id,
        )
        connection.commit()
        return response
    except ApiError as exc:
        _finish_auth_error(connection, exc)
        raise
    except (sqlite3.Error, ValueError, TypeError):
        _rollback(connection)
        raise ApiError("platform.unavailable") from None
    finally:
        connection.close()


def revoke_support_grant_platform(
    config: AppConfig,
    *,
    session_token: str,
    csrf_token: str,
    grant_id: str,
    reason_code: str,
    request_id: str,
    trace_id: str,
) -> SupportGrantResponse:
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        actor = _authenticate_platform(
            connection, config, session_token, csrf_token=csrf_token, touch=True
        )
        grant = _grant_row(connection, grant_id)
        if (
            grant["requester_operator_id"] != actor.operator_id
            and "platform.support.approve" not in actor.permissions
        ):
            raise ApiError("platform.permission_denied")
        now = _utcnow()
        _revoke_grant(connection, grant, now)
        _audit(
            connection,
            actor_domain="platform",
            actor_id=actor.operator_id,
            action="support.access_revoked",
            object_type="support_grant",
            object_id=grant_id,
            reason=reason_code,
            request_id=request_id,
            trace_id=trace_id,
            now=now,
            event_type="support.access_revoked",
        )
        response = _grant_response(connection, grant_id, request_id, trace_id, now)
        connection.commit()
        return response
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, ValueError, TypeError):
        _rollback(connection)
        raise ApiError("platform.unavailable") from None
    finally:
        connection.close()


def revoke_support_grant_workspace(
    config: AppConfig,
    *,
    workspace_session_token: str,
    workspace_csrf_token: str,
    grant_id: str,
    reason_code: str,
    request_id: str,
    trace_id: str,
) -> SupportGrantResponse:
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        actor = authenticate_workspace_session_in_transaction(
            connection,
            config,
            session_token=workspace_session_token,
            csrf_token=workspace_csrf_token,
            touch=True,
        )
        if actor.membership_role not in {"owner", "admin"}:
            raise ApiError("workspace_session.permission_denied")
        grant = _grant_row(connection, grant_id)
        if grant["workspace_id"] != actor.workspace_id:
            raise ApiError("support.grant_not_found")
        now = _utcnow()
        _revoke_grant(connection, grant, now)
        _audit(
            connection,
            actor_domain="workspace",
            actor_id=actor.membership_id,
            action="support.access_revoked",
            object_type="support_grant",
            object_id=grant_id,
            reason=reason_code,
            request_id=request_id,
            trace_id=trace_id,
            now=now,
            event_type="support.access_revoked",
        )
        response = _grant_response(connection, grant_id, request_id, trace_id, now)
        connection.commit()
        return response
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, ValueError, TypeError):
        _rollback(connection)
        raise ApiError("platform.unavailable") from None
    finally:
        connection.close()


def approve_support_grant_platform(
    config: AppConfig,
    *,
    session_token: str,
    csrf_token: str,
    grant_id: str,
    request_id: str,
    trace_id: str,
) -> SupportGrantResponse:
    if _GRANT_ID_RE.fullmatch(grant_id) is None:
        raise ApiError("support.grant_not_found")
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        actor = _authenticate_platform(
            connection, config, session_token, csrf_token=csrf_token, touch=True
        )
        _require_permission(actor, "platform.support.approve")
        grant = _grant_row(connection, grant_id)
        if grant["requester_operator_id"] == actor.operator_id:
            raise ApiError("platform.permission_denied")
        now = _utcnow()
        _ensure_grant_pending(grant, now)
        try:
            connection.execute(
                """
                INSERT INTO support_grant_approvals(
                  approval_id,grant_id,approval_domain,operator_id,membership_id,
                  authority_revision,created_at)
                VALUES(?,?,'platform',?,NULL,?,?)
                """,
                (_id("sap_"), grant_id, actor.operator_id, actor.operator_revision, _db_time(now)),
            )
        except sqlite3.IntegrityError:
            raise ApiError("support.grant_conflict") from None
        activated = _activate_if_approved(connection, grant_id, now)
        _audit_grant_approval(
            connection,
            actor.operator_id,
            grant_id,
            "platform",
            request_id,
            trace_id,
            now,
            activated=activated,
        )
        response = _grant_response(connection, grant_id, request_id, trace_id, now)
        connection.commit()
        return response
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, ValueError, TypeError):
        _rollback(connection)
        raise ApiError("platform.unavailable") from None
    finally:
        connection.close()


def approve_support_grant_workspace(
    config: AppConfig,
    *,
    workspace_session_token: str,
    workspace_csrf_token: str,
    grant_id: str,
    request_id: str,
    trace_id: str,
) -> SupportGrantResponse:
    if _GRANT_ID_RE.fullmatch(grant_id) is None:
        raise ApiError("support.grant_not_found")
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        actor = authenticate_workspace_session_in_transaction(
            connection,
            config,
            session_token=workspace_session_token,
            csrf_token=workspace_csrf_token,
            touch=True,
        )
        if actor.membership_role not in {"owner", "admin"}:
            raise ApiError("workspace_session.permission_denied")
        grant = _grant_row(connection, grant_id)
        if grant["workspace_id"] != actor.workspace_id:
            raise ApiError("support.grant_not_found")
        now = _utcnow()
        _ensure_grant_pending(grant, now)
        try:
            membership_revision = int(
                connection.execute(
                    "SELECT revision FROM workspace_memberships WHERE workspace_membership_id=?",
                    (actor.membership_id,),
                ).fetchone()[0]
            )
            connection.execute(
                """
                INSERT INTO support_grant_approvals(
                  approval_id,grant_id,approval_domain,operator_id,membership_id,
                  authority_revision,created_at)
                VALUES(?,?,'workspace',NULL,?,?,?)
                """,
                (_id("sap_"), grant_id, actor.membership_id, membership_revision, _db_time(now)),
            )
        except sqlite3.IntegrityError:
            raise ApiError("support.grant_conflict") from None
        activated = _activate_if_approved(connection, grant_id, now)
        _audit_grant_approval(
            connection,
            actor.membership_id,
            grant_id,
            "workspace",
            request_id,
            trace_id,
            now,
            activated=activated,
        )
        response = _grant_response(connection, grant_id, request_id, trace_id, now)
        connection.commit()
        return response
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, ValueError, TypeError):
        _rollback(connection)
        raise ApiError("platform.unavailable") from None
    finally:
        connection.close()


def issue_support_session(
    config: AppConfig,
    *,
    session_token: str,
    csrf_token: str,
    grant_id: str,
    request_id: str,
    trace_id: str,
) -> IssuedSupportSession:
    if _GRANT_ID_RE.fullmatch(grant_id) is None:
        raise ApiError("support.grant_not_found")
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        actor = _authenticate_platform(
            connection, config, session_token, csrf_token=csrf_token, touch=True
        )
        grant = _grant_row(connection, grant_id)
        now = _utcnow()
        if grant["requester_operator_id"] != actor.operator_id or grant["status"] != "active":
            raise ApiError("support.grant_conflict")
        _validate_grant_authority(connection, grant, now)
        raw_token = secrets.token_urlsafe(48)
        support_session_id = _id("sps_")
        expires = min(
            now + timedelta(seconds=config.support_session_ttl_seconds),
            _parse_time(grant["expires_at"]),
        )
        connection.execute(
            """
            INSERT INTO support_sessions(
              support_session_id,source_kind,source_id,operator_id,factor_id,workspace_id,tenant_id,
              scope_code,token_hmac,status,operator_revision,factor_revision,absolute_expires_at,
              revoked_at,created_at,updated_at)
            VALUES(?,'grant',?,?,?,?,?,'workspace.diagnostics.read',?,'active',?,?,?,NULL,?,?)
            """,
            (
                support_session_id,
                grant_id,
                actor.operator_id,
                actor.factor_id,
                grant["workspace_id"],
                grant["tenant_id"],
                _support_hmac(config, raw_token),
                actor.operator_revision,
                actor.factor_revision,
                _db_time(expires),
                _db_time(now),
                _db_time(now),
            ),
        )
        _audit(
            connection,
            actor_domain="platform",
            actor_id=actor.operator_id,
            action="support.session_issued",
            object_type="support_session",
            object_id=support_session_id,
            reason="grant_session",
            request_id=request_id,
            trace_id=trace_id,
            now=now,
            after={"grant_id": grant_id, "workspace_id": grant["workspace_id"]},
        )
        response = SupportSessionResponse(
            session=SupportSessionSummary(
                source_kind="grant",
                source_id=grant_id,
                workspace_id=grant["workspace_id"],
                expires_at=expires,
            ),
            request_id=request_id,
            trace_id=trace_id,
        )
        connection.commit()
        return IssuedSupportSession(response=response, session_token=raw_token)
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, ValueError, TypeError):
        _rollback(connection)
        raise ApiError("platform.unavailable") from None
    finally:
        connection.close()


def create_break_glass_session(
    config: AppConfig,
    *,
    session_token: str,
    csrf_token: str,
    workspace_id: str,
    reason_code: str,
    totp_code: str,
    device_fingerprint: str,
    request_id: str,
    trace_id: str,
) -> IssuedSupportSession:
    if _WORKSPACE_ID_RE.fullmatch(workspace_id) is None:
        raise ApiError("platform.request_invalid")
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        actor = _authenticate_platform(
            connection, config, session_token, csrf_token=csrf_token, touch=True
        )
        _require_permission(actor, "platform.break_glass.use")
        now = _utcnow()
        factor_id, factor_revision, _counter = consume_active_totp_in_transaction(
            connection,
            config,
            account_id=actor.account_id,
            code=totp_code,
            device_fingerprint=device_fingerprint,
            now=now,
        )
        workspace = connection.execute(
            """
            SELECT workspace.workspace_id,binding.tenant_id
            FROM workspaces workspace JOIN workspace_tenant_bindings binding
              ON binding.workspace_id=workspace.workspace_id
            WHERE workspace.workspace_id=? AND workspace.status='active' AND binding.status='active'
            """,
            (workspace_id,),
        ).fetchone()
        if workspace is None:
            raise ApiError("support.grant_not_found")
        break_glass_id = _id("bgs_")
        support_session_id = _id("sps_")
        expires = now + timedelta(minutes=10)
        review_due = expires + timedelta(hours=24)
        raw_token = secrets.token_urlsafe(48)
        connection.execute(
            """
            INSERT INTO break_glass_sessions(
              break_glass_id,requester_operator_id,workspace_id,tenant_id,reason_code,
              status,expires_at,review_due_at,created_at,updated_at)
            VALUES(?,?,?,?,?,'active',?,?,?,?)
            """,
            (
                break_glass_id,
                actor.operator_id,
                workspace_id,
                workspace["tenant_id"],
                reason_code,
                _db_time(expires),
                _db_time(review_due),
                _db_time(now),
                _db_time(now),
            ),
        )
        connection.execute(
            """
            INSERT INTO support_sessions(
              support_session_id,source_kind,source_id,operator_id,factor_id,workspace_id,tenant_id,
              scope_code,token_hmac,status,operator_revision,factor_revision,absolute_expires_at,
              revoked_at,created_at,updated_at)
            VALUES(?,'break_glass',?,?,?,?,?,'workspace.diagnostics.read',?,'active',?,?,?,NULL,?,?)
            """,
            (
                support_session_id,
                break_glass_id,
                actor.operator_id,
                factor_id,
                workspace_id,
                workspace["tenant_id"],
                _support_hmac(config, raw_token),
                actor.operator_revision,
                factor_revision,
                _db_time(expires),
                _db_time(now),
                _db_time(now),
            ),
        )
        _audit(
            connection,
            actor_domain="platform",
            actor_id=actor.operator_id,
            action="break_glass.used",
            object_type="break_glass_session",
            object_id=break_glass_id,
            reason=reason_code,
            request_id=request_id,
            trace_id=trace_id,
            now=now,
            event_type="break_glass.used",
            event_priority="high",
            after={"workspace_id": workspace_id, "review_due_at": _db_time(review_due)},
        )
        response = BreakGlassSessionResponse(
            break_glass_id=break_glass_id,
            session=SupportSessionSummary(
                source_kind="break_glass",
                source_id=break_glass_id,
                workspace_id=workspace_id,
                expires_at=expires,
            ),
            review_due_at=review_due,
            request_id=request_id,
            trace_id=trace_id,
        )
        connection.commit()
        return IssuedSupportSession(response=response, session_token=raw_token)
    except ApiError as exc:
        _finish_mfa_write_error(connection, exc)
        raise
    except (sqlite3.Error, ValueError, TypeError):
        _rollback(connection)
        raise ApiError("platform.unavailable") from None
    finally:
        connection.close()


def review_break_glass_platform(
    config: AppConfig,
    *,
    session_token: str,
    csrf_token: str,
    break_glass_id: str,
    request_id: str,
    trace_id: str,
) -> BreakGlassReviewResponse:
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        actor = _authenticate_platform(
            connection, config, session_token, csrf_token=csrf_token, touch=True
        )
        if not set(actor.roles) & {"platform_owner", "platform_security", "platform_compliance"}:
            raise ApiError("platform.permission_denied")
        emergency = _break_glass_row(connection, break_glass_id)
        if emergency["requester_operator_id"] == actor.operator_id:
            raise ApiError("platform.permission_denied")
        now = _utcnow()
        _require_review_window(connection, emergency, now)
        _insert_break_glass_review(
            connection,
            break_glass_id=break_glass_id,
            domain="platform",
            actor_id=actor.operator_id,
            authority_revision=actor.operator_revision,
            now=now,
        )
        _audit(
            connection,
            actor_domain="platform",
            actor_id=actor.operator_id,
            action="break_glass.reviewed",
            object_type="break_glass_session",
            object_id=break_glass_id,
            reason="platform_post_review",
            request_id=request_id,
            trace_id=trace_id,
            now=now,
        )
        response = _break_glass_review_response(connection, break_glass_id, request_id, trace_id)
        connection.commit()
        return response
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, ValueError, TypeError):
        _rollback(connection)
        raise ApiError("platform.unavailable") from None
    finally:
        connection.close()


def review_break_glass_workspace(
    config: AppConfig,
    *,
    workspace_session_token: str,
    workspace_csrf_token: str,
    break_glass_id: str,
    request_id: str,
    trace_id: str,
) -> BreakGlassReviewResponse:
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        actor = authenticate_workspace_session_in_transaction(
            connection,
            config,
            session_token=workspace_session_token,
            csrf_token=workspace_csrf_token,
            touch=True,
        )
        if actor.membership_role not in {"owner", "admin"}:
            raise ApiError("workspace_session.permission_denied")
        emergency = _break_glass_row(connection, break_glass_id)
        if emergency["workspace_id"] != actor.workspace_id:
            raise ApiError("support.grant_not_found")
        now = _utcnow()
        _require_review_window(connection, emergency, now)
        membership_revision = int(
            connection.execute(
                "SELECT revision FROM workspace_memberships WHERE workspace_membership_id=?",
                (actor.membership_id,),
            ).fetchone()[0]
        )
        _insert_break_glass_review(
            connection,
            break_glass_id=break_glass_id,
            domain="workspace",
            actor_id=actor.membership_id,
            authority_revision=membership_revision,
            now=now,
        )
        _audit(
            connection,
            actor_domain="workspace",
            actor_id=actor.membership_id,
            action="break_glass.reviewed",
            object_type="break_glass_session",
            object_id=break_glass_id,
            reason="workspace_post_review",
            request_id=request_id,
            trace_id=trace_id,
            now=now,
        )
        response = _break_glass_review_response(connection, break_glass_id, request_id, trace_id)
        connection.commit()
        return response
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, ValueError, TypeError):
        _rollback(connection)
        raise ApiError("platform.unavailable") from None
    finally:
        connection.close()


def get_support_session(
    config: AppConfig, *, session_token: str, request_id: str, trace_id: str
) -> SupportSessionResponse:
    connection = _connect(config)
    try:
        connection.execute("BEGIN")
        row = _authenticate_support(connection, config, session_token)
        response = SupportSessionResponse(
            session=SupportSessionSummary(
                source_kind=row["source_kind"],
                source_id=row["source_id"],
                workspace_id=row["workspace_id"],
                expires_at=_parse_time(row["absolute_expires_at"]),
            ),
            request_id=request_id,
            trace_id=trace_id,
        )
        connection.commit()
        return response
    except ApiError as exc:
        _finish_support_error(connection, exc)
        raise
    except (sqlite3.Error, ValueError, TypeError):
        _rollback(connection)
        raise ApiError("platform.unavailable") from None
    finally:
        connection.close()


def get_support_diagnostics(
    config: AppConfig, *, session_token: str, request_id: str, trace_id: str
) -> SupportDiagnosticsResponse:
    connection = _connect(config)
    try:
        connection.execute("BEGIN")
        session = _authenticate_support(connection, config, session_token)
        workspace = connection.execute(
            """
            SELECT workspace.status workspace_status,binding.status binding_status,
              tenant.status tenant_status,region.region_code
            FROM workspaces workspace
            JOIN workspace_tenant_bindings binding ON binding.workspace_id=workspace.workspace_id
            JOIN tenants tenant ON tenant.tenant_id=binding.tenant_id
            LEFT JOIN workspace_regions region ON region.workspace_id=workspace.workspace_id
            WHERE workspace.workspace_id=? AND binding.tenant_id=?
            """,
            (session["workspace_id"], session["tenant_id"]),
        ).fetchone()
        if workspace is None:
            raise ApiError("support.session_invalid")
        membership_count = int(
            connection.execute(
                """
                SELECT COUNT(*) FROM workspace_memberships
                WHERE workspace_id=? AND status='active'
                """,
                (session["workspace_id"],),
            ).fetchone()[0]
        )
        health_band = None
        if _table_exists(connection, "customer_health_scores"):
            health = connection.execute(
                """
                SELECT risk_level FROM customer_health_scores
                WHERE tenant_id=? ORDER BY customer_health_score_sequence DESC LIMIT 1
                """,
                (session["tenant_id"],),
            ).fetchone()
            health_band = str(health[0]) if health is not None else None
        operational = (
            "active"
            if workspace["workspace_status"] == "active" and workspace["tenant_status"] == "active"
            else "restricted"
        )
        response = SupportDiagnosticsResponse(
            workspace_id=session["workspace_id"],
            operational_status=operational,
            region_code=workspace["region_code"],
            provisioning_status=(
                "ready" if workspace["binding_status"] == "active" else "attention_required"
            ),
            health_band=health_band,
            active_memberships=membership_count,
            request_id=request_id,
            trace_id=trace_id,
        )
        connection.commit()
        return response
    except ApiError as exc:
        _finish_support_error(connection, exc)
        raise
    except (sqlite3.Error, ValueError, TypeError):
        _rollback(connection)
        raise ApiError("platform.unavailable") from None
    finally:
        connection.close()


def list_platform_audit(
    config: AppConfig,
    *,
    session_token: str,
    limit: int,
    cursor: str | None,
    request_id: str,
    trace_id: str,
) -> PlatformAuditListResponse:
    connection = _connect(config)
    try:
        connection.execute("BEGIN")
        context = _authenticate_platform(connection, config, session_token, touch=True)
        _require_permission(context, "platform.audit.read")
        scope, predicate, parameters = _audit_scope(context)
        boundary = _decode_audit_cursor(config, context, scope, cursor) if cursor else None
        if boundary is not None:
            predicate += " AND (audit.occurred_at<? OR (audit.occurred_at=? AND audit.audit_id<?))"
            parameters.extend((boundary[0], boundary[0], boundary[1]))
        rows = connection.execute(
            f"""
            SELECT audit.audit_id,audit.action_code,audit.object_type,audit.object_id,
              audit.reason_code,audit.occurred_at
            FROM platform_audit_logs audit
            WHERE {predicate}
            ORDER BY audit.occurred_at DESC,audit.audit_id DESC LIMIT ?
            """,
            (*parameters, limit + 1),
        ).fetchall()
        visible = rows[:limit]
        next_cursor = None
        if len(rows) > limit:
            last = visible[-1]
            next_cursor = _encode_audit_cursor(
                config,
                context,
                scope,
                occurred_at=last["occurred_at"],
                audit_id=last["audit_id"],
            )
        count_predicate, count_parameters = _audit_scope(context)[1:]
        total = int(
            connection.execute(
                f"SELECT COUNT(*) FROM platform_audit_logs audit WHERE {count_predicate}",
                count_parameters,
            ).fetchone()[0]
        )
        response = PlatformAuditListResponse(
            entries=tuple(
                PlatformAuditSummary(
                    audit_id=row["audit_id"],
                    action=row["action_code"],
                    object_type=row["object_type"],
                    object_id=row["object_id"],
                    reason_code=row["reason_code"],
                    occurred_at=_parse_time(row["occurred_at"]),
                )
                for row in visible
            ),
            total=total,
            limit=limit,
            next_cursor=next_cursor,
            request_id=request_id,
            trace_id=trace_id,
        )
        connection.commit()
        return response
    except ApiError as exc:
        _finish_auth_error(connection, exc)
        raise
    except (sqlite3.Error, ValueError, TypeError):
        _rollback(connection)
        raise ApiError("platform.unavailable") from None
    finally:
        connection.close()


def _audit_scope(context: PlatformContext) -> tuple[str, str, list[str]]:
    if "platform_owner" in context.roles:
        return "all", "1=1", []
    if {"platform_security", "platform_compliance"} & set(context.roles):
        placeholders = ",".join("?" for _ in _SECURITY_AUDIT_ACTIONS)
        return (
            "security",
            f"audit.action_code IN ({placeholders})",
            list(_SECURITY_AUDIT_ACTIONS),
        )
    if "platform_support" in context.roles:
        return (
            "own_support_grants",
            "audit.object_type='support_grant' AND EXISTS("
            "SELECT 1 FROM support_grants grant_record "
            "WHERE grant_record.grant_id=audit.object_id "
            "AND grant_record.requester_operator_id=?)",
            [context.operator_id],
        )
    raise ApiError("platform.permission_denied")


def _encode_audit_cursor(
    config: AppConfig,
    context: PlatformContext,
    scope: str,
    *,
    occurred_at: str,
    audit_id: str,
) -> str:
    payload = json.dumps(
        {
            "audit_id": audit_id,
            "direction": "next",
            "occurred_at": occurred_at,
            "operator_revision": context.operator_revision,
            "platform_session_id": context.platform_session_id,
            "role_digest": _audit_role_digest(context),
            "scope": scope,
            "v": 1,
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    signature = hmac.new(
        config.platform_session_secret.encode("utf-8"),
        _AUDIT_CURSOR_DOMAIN + b"\0" + payload,
        hashlib.sha256,
    ).digest()
    return f"{_base64_encode(payload)}.{_base64_encode(signature)}"


def _decode_audit_cursor(
    config: AppConfig,
    context: PlatformContext,
    scope: str,
    cursor: str,
) -> tuple[str, str]:
    try:
        if not 20 <= len(cursor) <= 2048 or cursor.count(".") != 1:
            raise ValueError
        encoded_payload, encoded_signature = cursor.split(".", 1)
        payload = _base64_decode(encoded_payload)
        signature = _base64_decode(encoded_signature)
        expected = hmac.new(
            config.platform_session_secret.encode("utf-8"),
            _AUDIT_CURSOR_DOMAIN + b"\0" + payload,
            hashlib.sha256,
        ).digest()
        if len(signature) != hashlib.sha256().digest_size or not hmac.compare_digest(
            signature, expected
        ):
            raise ValueError
        parsed = json.loads(payload.decode("ascii"))
        expected_fields = {
            "audit_id",
            "direction",
            "occurred_at",
            "operator_revision",
            "platform_session_id",
            "role_digest",
            "scope",
            "v",
        }
        if not isinstance(parsed, dict) or set(parsed) != expected_fields:
            raise ValueError
        canonical = json.dumps(
            parsed,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
        if canonical != payload or (
            parsed["v"] != 1
            or parsed["direction"] != "next"
            or parsed["platform_session_id"] != context.platform_session_id
            or parsed["operator_revision"] != context.operator_revision
            or parsed["role_digest"] != _audit_role_digest(context)
            or parsed["scope"] != scope
            or not _AUDIT_ID_RE.fullmatch(str(parsed["audit_id"]))
        ):
            raise ValueError
        occurred_at = str(parsed["occurred_at"])
        _parse_time(occurred_at)
        return occurred_at, str(parsed["audit_id"])
    except (binascii.Error, UnicodeError, ValueError, TypeError, json.JSONDecodeError):
        raise ApiError("platform.request_invalid") from None


def _audit_role_digest(context: PlatformContext) -> str:
    return hashlib.sha256(
        json.dumps(context.roles, ensure_ascii=True, separators=(",", ":")).encode("ascii")
    ).hexdigest()


def _base64_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _base64_decode(value: str) -> bytes:
    if not value or "=" in value or re.fullmatch(r"[A-Za-z0-9_-]+", value) is None:
        raise ValueError
    decoded = base64.b64decode(
        value + "=" * ((4 - len(value) % 4) % 4), altchars=b"-_", validate=True
    )
    if _base64_encode(decoded) != value:
        raise ValueError
    return decoded


def _authenticate_platform(
    connection: sqlite3.Connection,
    config: AppConfig,
    session_token: str,
    *,
    csrf_token: str | None = None,
    touch: bool = False,
) -> PlatformContext:
    if not valid_opaque_value(session_token):
        raise ApiError("platform.session_invalid")
    row = connection.execute(
        """
        SELECT session.*,operator.status operator_status,
          operator.revision current_operator_revision,
          account.status account_status,account.login_enabled,
          global_session.status global_status,global_session.account_id global_account_id,
          global_session.expires_at global_expires_at,
          global_session.idle_expires_at global_idle_expires_at,
          factor.status factor_status,factor.revision current_factor_revision
        FROM platform_sessions session
        JOIN platform_operators operator ON operator.operator_id=session.operator_id
        JOIN global_accounts account ON account.account_id=session.account_id
        JOIN global_sessions global_session ON global_session.session_id=session.global_session_id
        JOIN account_mfa_factors factor ON factor.factor_id=session.factor_id
        WHERE session.token_hmac=?
        """,
        (_platform_hmac(config, "token", session_token),),
    ).fetchone()
    if row is None:
        raise ApiError("platform.session_invalid")
    if row["status"] == "revoked":
        raise ApiError("platform.session_revoked")
    now = _utcnow()
    if row["status"] == "expired" or any(
        _parse_time(row[field]) <= now
        for field in (
            "absolute_expires_at",
            "idle_expires_at",
            "global_expires_at",
            "global_idle_expires_at",
        )
    ):
        connection.execute(
            """
            UPDATE platform_sessions SET status='expired',updated_at=?
            WHERE platform_session_id=? AND status='active'
            """,
            (_db_time(now), row["platform_session_id"]),
        )
        raise ApiError("platform.session_expired")
    roles, permissions, fingerprint = _authority(connection, row["operator_id"])
    stale = (
        row["operator_status"] != "active"
        or row["account_status"] != "active"
        or row["login_enabled"] != 1
        or row["global_status"] != "active"
        or row["global_account_id"] != row["account_id"]
        or row["factor_status"] != "active"
        or row["operator_revision"] != row["current_operator_revision"]
        or row["factor_revision"] != row["current_factor_revision"]
        or not roles
        or not hmac.compare_digest(row["role_fingerprint"], fingerprint)
    )
    if stale:
        connection.execute(
            """
            UPDATE platform_sessions SET status='revoked',revoked_at=?,
              revoked_reason='authority_changed',updated_at=?
            WHERE platform_session_id=? AND status='active'
            """,
            (_db_time(now), _db_time(now), row["platform_session_id"]),
        )
        raise ApiError("platform.session_revoked")
    if csrf_token is not None and (
        not valid_opaque_value(csrf_token)
        or not hmac.compare_digest(row["csrf_hmac"], _platform_hmac(config, "csrf", csrf_token))
    ):
        raise ApiError("platform.csrf_invalid")
    if touch and _parse_time(row["last_seen_at"]) <= now - timedelta(seconds=60):
        connection.execute(
            "UPDATE platform_sessions SET last_seen_at=?,updated_at=? WHERE platform_session_id=?",
            (_db_time(now), _db_time(now), row["platform_session_id"]),
        )
    return PlatformContext(
        platform_session_id=row["platform_session_id"],
        operator_id=row["operator_id"],
        account_id=row["account_id"],
        global_session_id=row["global_session_id"],
        factor_id=row["factor_id"],
        factor_revision=int(row["factor_revision"]),
        operator_revision=int(row["operator_revision"]),
        roles=roles,
        permissions=permissions,
        expires_at=_parse_time(row["absolute_expires_at"]),
    )


def _authenticate_support(
    connection: sqlite3.Connection, config: AppConfig, session_token: str
) -> sqlite3.Row:
    if not valid_opaque_value(session_token):
        raise ApiError("support.session_invalid")
    row = connection.execute(
        """
        SELECT session.*,operator.status operator_status,
          operator.account_id operator_account_id,
          operator.revision current_operator_revision,
          account.status account_status,account.login_enabled,
          factor.account_id factor_account_id,factor.status factor_status,
          factor.revision current_factor_revision
        FROM support_sessions session
        JOIN platform_operators operator ON operator.operator_id=session.operator_id
        JOIN global_accounts account ON account.account_id=operator.account_id
        JOIN account_mfa_factors factor ON factor.factor_id=session.factor_id
        WHERE session.token_hmac=?
        """,
        (_support_hmac(config, session_token),),
    ).fetchone()
    if row is None or row["status"] == "revoked":
        raise ApiError("support.session_invalid")
    now = _utcnow()
    if row["status"] == "expired" or _parse_time(row["absolute_expires_at"]) <= now:
        connection.execute(
            "UPDATE support_sessions SET status='expired',updated_at=? WHERE support_session_id=?",
            (_db_time(now), row["support_session_id"]),
        )
        if row["source_kind"] == "break_glass":
            connection.execute(
                """
                UPDATE break_glass_sessions SET status='review_pending',updated_at=?
                WHERE break_glass_id=? AND status='active'
                """,
                (_db_time(now), row["source_id"]),
            )
        raise ApiError("support.session_expired")
    if (
        row["operator_status"] != "active"
        or row["account_status"] != "active"
        or row["login_enabled"] != 1
        or row["factor_account_id"] != row["operator_account_id"]
        or row["operator_revision"] != row["current_operator_revision"]
        or row["factor_status"] != "active"
        or row["factor_revision"] != row["current_factor_revision"]
    ):
        _revoke_support_session(connection, row["support_session_id"], now)
        raise ApiError("support.session_invalid")
    if row["source_kind"] == "grant":
        try:
            grant = _grant_row(connection, row["source_id"])
        except ApiError:
            _revoke_support_session(connection, row["support_session_id"], now)
            raise ApiError("support.session_invalid") from None
        if grant["status"] != "active":
            _revoke_support_session(connection, row["support_session_id"], now)
            raise ApiError("support.session_invalid")
        try:
            _validate_grant_authority(connection, grant, now)
        except ApiError:
            _revoke_support_session(connection, row["support_session_id"], now)
            raise ApiError("support.session_invalid") from None
    else:
        emergency = connection.execute(
            "SELECT * FROM break_glass_sessions WHERE break_glass_id=?",
            (row["source_id"],),
        ).fetchone()
        if (
            emergency is None
            or emergency["status"] != "active"
            or _parse_time(emergency["expires_at"]) <= now
        ):
            if emergency is not None and emergency["status"] == "active":
                connection.execute(
                    """
                    UPDATE break_glass_sessions SET status='review_pending',updated_at=?
                    WHERE break_glass_id=? AND status='active'
                    """,
                    (_db_time(now), row["source_id"]),
                )
            _revoke_support_session(connection, row["support_session_id"], now)
            raise ApiError("support.session_invalid")
    return row


def _revoke_support_session(
    connection: sqlite3.Connection, support_session_id: str, now: datetime
) -> None:
    connection.execute(
        """
        UPDATE support_sessions SET status='revoked',revoked_at=?,updated_at=?
        WHERE support_session_id=? AND status='active'
        """,
        (_db_time(now), _db_time(now), support_session_id),
    )


def _validate_grant_authority(
    connection: sqlite3.Connection, grant: sqlite3.Row, now: datetime
) -> None:
    if _parse_time(grant["expires_at"]) <= now:
        connection.execute(
            "UPDATE support_grants SET status='expired',updated_at=? WHERE grant_id=?",
            (_db_time(now), grant["grant_id"]),
        )
        raise ApiError("support.grant_conflict")
    requester = connection.execute(
        "SELECT status,revision FROM platform_operators WHERE operator_id=?",
        (grant["requester_operator_id"],),
    ).fetchone()
    workspace = connection.execute(
        """
        SELECT status,revision FROM workspaces WHERE workspace_id=?
        """,
        (grant["workspace_id"],),
    ).fetchone()
    if (
        requester is None
        or requester["status"] != "active"
        or requester["revision"] != grant["operator_revision"]
        or workspace is None
        or workspace["status"] != "active"
        or workspace["revision"] != grant["workspace_revision"]
    ):
        raise ApiError("support.grant_conflict")
    approvals = connection.execute(
        "SELECT * FROM support_grant_approvals WHERE grant_id=?", (grant["grant_id"],)
    ).fetchall()
    if {row["approval_domain"] for row in approvals} != {"workspace", "platform"}:
        raise ApiError("support.grant_conflict")
    for approval in approvals:
        if approval["approval_domain"] == "platform":
            valid = connection.execute(
                """
                SELECT 1 FROM platform_operators operator
                JOIN platform_operator_role_bindings binding
                  ON binding.operator_id=operator.operator_id AND binding.status='active'
                WHERE operator.operator_id=? AND operator.status='active'
                  AND operator.revision=? AND binding.role_code IN(
                    'platform_owner','platform_security','platform_compliance')
                """,
                (approval["operator_id"], approval["authority_revision"]),
            ).fetchone()
        else:
            valid = connection.execute(
                """
                SELECT 1 FROM workspace_memberships
                WHERE workspace_membership_id=? AND workspace_id=? AND status='active'
                  AND revision=? AND role_code IN('owner','admin')
                """,
                (
                    approval["membership_id"],
                    grant["workspace_id"],
                    approval["authority_revision"],
                ),
            ).fetchone()
        if valid is None:
            raise ApiError("support.grant_conflict")


def _grant_row(connection: sqlite3.Connection, grant_id: str) -> sqlite3.Row:
    if _GRANT_ID_RE.fullmatch(grant_id) is None:
        raise ApiError("support.grant_not_found")
    row = connection.execute(
        "SELECT * FROM support_grants WHERE grant_id=?",
        (grant_id,),
    ).fetchone()
    if row is None:
        raise ApiError("support.grant_not_found")
    return row


def _ensure_grant_pending(grant: sqlite3.Row, now: datetime) -> None:
    if grant["status"] != "pending_approvals" or _parse_time(grant["expires_at"]) <= now:
        raise ApiError("support.grant_conflict")


def _activate_if_approved(connection: sqlite3.Connection, grant_id: str, now: datetime) -> bool:
    count = int(
        connection.execute(
            "SELECT COUNT(DISTINCT approval_domain) FROM support_grant_approvals WHERE grant_id=?",
            (grant_id,),
        ).fetchone()[0]
    )
    if count == 2:
        updated = connection.execute(
            """
            UPDATE support_grants SET status='active',updated_at=?
            WHERE grant_id=? AND status='pending_approvals' AND expires_at>?
            """,
            (_db_time(now), grant_id, _db_time(now)),
        )
        return updated.rowcount == 1
    return False


def _grant_response(
    connection: sqlite3.Connection,
    grant_id: str,
    request_id: str,
    trace_id: str,
    now: datetime,
) -> SupportGrantResponse:
    row = _grant_row(connection, grant_id)
    if row["status"] in {"pending_approvals", "active"} and _parse_time(row["expires_at"]) <= now:
        connection.execute(
            "UPDATE support_grants SET status='expired',updated_at=? WHERE grant_id=?",
            (_db_time(now), grant_id),
        )
        row = _grant_row(connection, grant_id)
    return SupportGrantResponse(
        grant=_grant_summary(connection, row, now),
        request_id=request_id,
        trace_id=trace_id,
    )


def _grant_summary(
    connection: sqlite3.Connection, row: sqlite3.Row, now: datetime
) -> SupportGrantSummary:
    domains = {
        approval[0]
        for approval in connection.execute(
            "SELECT approval_domain FROM support_grant_approvals WHERE grant_id=?",
            (row["grant_id"],),
        ).fetchall()
    }
    missing = tuple(domain for domain in ("workspace", "platform") if domain not in domains)
    status = row["status"]
    if status in {"pending_approvals", "active"} and _parse_time(row["expires_at"]) <= now:
        status = "expired"
    return SupportGrantSummary(
        grant_id=row["grant_id"],
        workspace_id=row["workspace_id"],
        status=status,
        missing_approvals=missing,
        expires_at=_parse_time(row["expires_at"]),
    )


def _revoke_grant(connection: sqlite3.Connection, grant: sqlite3.Row, now: datetime) -> None:
    if grant["status"] not in {"pending_approvals", "active"}:
        raise ApiError("support.grant_conflict")
    updated = connection.execute(
        """
        UPDATE support_grants SET status='revoked',revoked_at=?,updated_at=?
        WHERE grant_id=? AND status IN('pending_approvals','active')
        """,
        (_db_time(now), _db_time(now), grant["grant_id"]),
    ).rowcount
    if updated != 1:
        raise ApiError("support.grant_conflict")
    connection.execute(
        """
        UPDATE support_sessions SET status='revoked',revoked_at=?,updated_at=?
        WHERE source_kind='grant' AND source_id=? AND status='active'
        """,
        (_db_time(now), _db_time(now), grant["grant_id"]),
    )


def _break_glass_row(connection: sqlite3.Connection, break_glass_id: str) -> sqlite3.Row:
    if _BREAK_GLASS_ID_RE.fullmatch(break_glass_id) is None:
        raise ApiError("support.grant_not_found")
    row = connection.execute(
        "SELECT * FROM break_glass_sessions WHERE break_glass_id=?", (break_glass_id,)
    ).fetchone()
    if row is None:
        raise ApiError("support.grant_not_found")
    return row


def _require_review_window(
    connection: sqlite3.Connection, emergency: sqlite3.Row, now: datetime
) -> None:
    if _parse_time(emergency["expires_at"]) > now or emergency["status"] == "reviewed":
        raise ApiError("support.grant_conflict")
    if emergency["status"] in {"active", "expired"}:
        connection.execute(
            """
            UPDATE break_glass_sessions SET status='review_pending',updated_at=?
            WHERE break_glass_id=?
            """,
            (_db_time(now), emergency["break_glass_id"]),
        )


def _insert_break_glass_review(
    connection: sqlite3.Connection,
    *,
    break_glass_id: str,
    domain: str,
    actor_id: str,
    authority_revision: int,
    now: datetime,
) -> None:
    try:
        connection.execute(
            """
            INSERT INTO break_glass_reviews(
              review_id,break_glass_id,review_domain,operator_id,membership_id,
              authority_revision,created_at)
            VALUES(?,?,?,CASE WHEN ?='platform' THEN ? END,
              CASE WHEN ?='workspace' THEN ? END,?,?)
            """,
            (
                _id("bgr_"),
                break_glass_id,
                domain,
                domain,
                actor_id,
                domain,
                actor_id,
                authority_revision,
                _db_time(now),
            ),
        )
    except sqlite3.IntegrityError:
        raise ApiError("support.grant_conflict") from None
    if (
        int(
            connection.execute(
                """
                SELECT COUNT(DISTINCT review_domain)
                FROM break_glass_reviews WHERE break_glass_id=?
                """,
                (break_glass_id,),
            ).fetchone()[0]
        )
        == 2
    ):
        connection.execute(
            """
            UPDATE break_glass_sessions SET status='reviewed',updated_at=?
            WHERE break_glass_id=? AND status='review_pending'
            """,
            (_db_time(now), break_glass_id),
        )


def _break_glass_review_response(
    connection: sqlite3.Connection,
    break_glass_id: str,
    request_id: str,
    trace_id: str,
) -> BreakGlassReviewResponse:
    row = _break_glass_row(connection, break_glass_id)
    reviewed = {
        result[0]
        for result in connection.execute(
            "SELECT review_domain FROM break_glass_reviews WHERE break_glass_id=?",
            (break_glass_id,),
        ).fetchall()
    }
    return BreakGlassReviewResponse(
        break_glass_id=break_glass_id,
        status=row["status"],
        missing_reviews=tuple(
            domain for domain in ("workspace", "platform") if domain not in reviewed
        ),
        review_due_at=_parse_time(row["review_due_at"]),
        request_id=request_id,
        trace_id=trace_id,
    )


def _audit_grant_approval(
    connection: sqlite3.Connection,
    actor_id: str,
    grant_id: str,
    domain: str,
    request_id: str,
    trace_id: str,
    now: datetime,
    *,
    activated: bool,
) -> None:
    _audit(
        connection,
        actor_domain=domain,
        actor_id=actor_id,
        action="support.access_approved",
        object_type="support_grant",
        object_id=grant_id,
        reason=f"{domain}_approval",
        request_id=request_id,
        trace_id=trace_id,
        now=now,
        event_type="support.access_granted" if activated else None,
        after={"approval_domain": domain},
    )


def _operator_summary(connection: sqlite3.Connection, operator_id: str) -> PlatformOperatorSummary:
    row = connection.execute(
        """
        SELECT operator.operator_id,operator.status,operator.revision,account.display_name
        FROM platform_operators operator
        JOIN global_accounts account ON account.account_id=operator.account_id
        WHERE operator.operator_id=?
        """,
        (operator_id,),
    ).fetchone()
    if row is None:
        raise ApiError("platform.operator_not_found")
    roles, _permissions, _fingerprint = _authority(connection, operator_id)
    return PlatformOperatorSummary(
        operator_id=row["operator_id"],
        display_name=row["display_name"],
        status=row["status"],
        roles=roles,
        revision=int(row["revision"]),
    )


def _authority(
    connection: sqlite3.Connection, operator_id: str
) -> tuple[tuple[str, ...], tuple[str, ...], str]:
    roles = tuple(
        row[0]
        for row in connection.execute(
            """
            SELECT role_code FROM platform_operator_role_bindings
            WHERE operator_id=? AND status='active' ORDER BY role_code
            """,
            (operator_id,),
        ).fetchall()
    )
    extension_queries: list[str] = []
    if _table_exists(connection, "platform_role_permission_extensions"):
        extension_queries.append(
            """
            SELECT extension.permission_code
            FROM platform_operator_role_bindings binding
            JOIN platform_role_permission_extensions extension
              ON extension.role_code=binding.role_code
            WHERE binding.operator_id=? AND binding.status='active'
            """
        )
    if _table_exists(connection, "platform_billing_role_permissions"):
        extension_queries.append(
            """
            SELECT extension.permission_code
            FROM platform_operator_role_bindings binding
            JOIN platform_billing_role_permissions extension
              ON extension.role_code=binding.role_code
            WHERE binding.operator_id=? AND binding.status='active'
            """
        )
    if _table_exists(connection, "platform_infrastructure_role_permissions"):
        extension_queries.append(
            """
            SELECT extension.permission_code
            FROM platform_operator_role_bindings binding
            JOIN platform_infrastructure_role_permissions extension
              ON extension.role_code=binding.role_code
            WHERE binding.operator_id=? AND binding.status='active'
            """
        )
    if _table_exists(connection, "platform_gateway_role_permissions"):
        extension_queries.append(
            """
            SELECT extension.permission_code
            FROM platform_operator_role_bindings binding
            JOIN platform_gateway_role_permissions extension
              ON extension.role_code=binding.role_code
            WHERE binding.operator_id=? AND binding.status='active'
            """
        )
    if _table_exists(connection, "platform_verification_role_permissions"):
        extension_queries.append(
            """
            SELECT extension.permission_code
            FROM platform_operator_role_bindings binding
            JOIN platform_verification_role_permissions extension
              ON extension.role_code=binding.role_code
            WHERE binding.operator_id=? AND binding.status='active'
            """
        )
    if _table_exists(connection, "platform_notification_role_permissions"):
        extension_queries.append(
            """
            SELECT extension.permission_code
            FROM platform_operator_role_bindings binding
            JOIN platform_notification_role_permissions extension
              ON extension.role_code=binding.role_code
            WHERE binding.operator_id=? AND binding.status='active'
            """
        )
    if _table_exists(connection, "platform_job_role_permissions"):
        extension_queries.append(
            """
            SELECT extension.permission_code
            FROM platform_operator_role_bindings binding
            JOIN platform_job_role_permissions extension
              ON extension.role_code=binding.role_code
            WHERE binding.operator_id=? AND binding.status='active'
            """
        )
    if _table_exists(connection, "platform_feature_role_permissions"):
        extension_queries.append(
            """
            SELECT extension.permission_code
            FROM platform_operator_role_bindings binding
            JOIN platform_feature_role_permissions extension
              ON extension.role_code=binding.role_code
            WHERE binding.operator_id=? AND binding.status='active'
            """
        )
    if _table_exists(connection, "platform_config_role_permissions"):
        extension_queries.append(
            """
            SELECT extension.permission_code
            FROM platform_operator_role_bindings binding
            JOIN platform_config_role_permissions extension
              ON extension.role_code=binding.role_code
            WHERE binding.operator_id=? AND binding.status='active'
            """
        )
    if _table_exists(connection, "platform_observability_role_permissions"):
        extension_queries.append(
            """
            SELECT extension.permission_code
            FROM platform_operator_role_bindings binding
            JOIN platform_observability_role_permissions extension
              ON extension.role_code=binding.role_code
            WHERE binding.operator_id=? AND binding.status='active'
            """
        )
    if _table_exists(connection, "platform_risk_role_permissions"):
        extension_queries.append(
            """
            SELECT extension.permission_code
            FROM platform_operator_role_bindings binding
            JOIN platform_risk_role_permissions extension
              ON extension.role_code=binding.role_code
            WHERE binding.operator_id=? AND binding.status='active'
            """
        )
    if _table_exists(connection, "platform_dr_role_permissions"):
        extension_queries.append(
            """
            SELECT extension.permission_code
            FROM platform_operator_role_bindings binding
            JOIN platform_dr_role_permissions extension
              ON extension.role_code=binding.role_code
            WHERE binding.operator_id=? AND binding.status='active'
            """
        )
    extension = "".join(f"\nUNION\n{query}" for query in extension_queries)
    parameters: tuple[object, ...] = tuple([operator_id, *([operator_id] * len(extension_queries))])
    permissions = tuple(
        row[0]
        for row in connection.execute(
            f"""
            SELECT permission.permission_code
            FROM platform_operator_role_bindings binding
            JOIN platform_role_permissions permission ON permission.role_code=binding.role_code
            WHERE binding.operator_id=? AND binding.status='active'
            {extension}
            ORDER BY permission_code
            """,
            parameters,
        ).fetchall()
    )
    fingerprint = hashlib.sha256(
        json.dumps([roles, permissions], separators=(",", ":")).encode("ascii")
    ).hexdigest()
    return roles, permissions, fingerprint


def _require_permission(context: PlatformContext, permission: str) -> None:
    if permission not in context.permissions:
        raise ApiError("platform.permission_denied")


def _verify_current_password(
    connection: sqlite3.Connection, account_id: str, current_password: str
) -> None:
    credential = connection.execute(
        """
        SELECT secret_hash FROM account_credentials
        WHERE account_id=? AND credential_type='password' AND status='active'
        """,
        (account_id,),
    ).fetchone()
    if credential is None or not verify_password(current_password, credential["secret_hash"]):
        raise ApiError("identity.reauthentication_failed")


def _checked_roles(roles: tuple[str, ...]) -> tuple[str, ...]:
    checked = tuple(sorted(set(roles)))
    if (
        not checked
        or len(checked) != len(roles)
        or any(role not in _ROLE_CODES for role in checked)
    ):
        raise ApiError("platform.request_invalid")
    return checked


def _active_owner_count(connection: sqlite3.Connection) -> int:
    return int(
        connection.execute(
            """
            SELECT COUNT(*) FROM platform_operators operator
            JOIN platform_operator_role_bindings binding ON binding.operator_id=operator.operator_id
            WHERE operator.status='active' AND binding.status='active'
              AND binding.role_code='platform_owner'
            """
        ).fetchone()[0]
    )


def _audit(
    connection: sqlite3.Connection,
    *,
    actor_domain: str,
    actor_id: str,
    action: str,
    object_type: str,
    object_id: str,
    reason: str,
    request_id: str,
    trace_id: str,
    now: datetime,
    event_type: str | None = None,
    event_priority: str = "normal",
    before: object | None = None,
    after: object | None = None,
) -> None:
    audit_id = _id("pau_")
    connection.execute(
        """
        INSERT INTO platform_audit_logs(
          audit_id,actor_domain,actor_id,action_code,object_type,object_id,reason_code,
          request_id,trace_id,before_sha256,after_sha256,occurred_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            audit_id,
            actor_domain,
            actor_id,
            action,
            object_type,
            object_id,
            reason,
            request_id,
            trace_id,
            _digest(before),
            _digest(after),
            _db_time(now),
        ),
    )
    if event_type is not None:
        connection.execute(
            """
            INSERT INTO platform_event_outbox(
              outbox_id,audit_id,event_type,priority,status,attempt_count,
              next_attempt_at,created_at,updated_at)
            VALUES(?,?,?,?,'pending',0,?,?,?)
            """,
            (
                _id("peo_"),
                audit_id,
                event_type,
                event_priority,
                _db_time(now),
                _db_time(now),
                _db_time(now),
            ),
        )


def _digest(value: object | None) -> str | None:
    if value is None:
        return None
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("ascii")
    ).hexdigest()


def _platform_hmac(config: AppConfig, purpose: str, value: str) -> str:
    return hmac.new(
        config.platform_session_secret.encode("utf-8"),
        f"prd095/platform/{purpose}\0{value}".encode(),
        hashlib.sha256,
    ).hexdigest()


def _support_hmac(config: AppConfig, value: str) -> str:
    return hmac.new(
        config.support_session_secret.encode("utf-8"),
        b"prd095/support/token\0" + value.encode("ascii"),
        hashlib.sha256,
    ).hexdigest()


def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
    if getattr(connection, "backend", "sqlite") == "postgresql":
        return connection.execute("SELECT to_regclass(?)", (name,)).fetchone()[0] is not None
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_schema WHERE type='table' AND name=?", (name,)
        ).fetchone()
        is not None
    )


def _connect(config: AppConfig) -> sqlite3.Connection:
    try:
        return connect_database(config.database_url)
    except Exception:
        raise ApiError("platform.unavailable") from None


def _finish_auth_error(connection: sqlite3.Connection, error: ApiError) -> None:
    if not connection.in_transaction:
        return
    if error.code in {"platform.session_expired", "platform.session_revoked"}:
        connection.commit()
    else:
        connection.rollback()


def _finish_mfa_write_error(connection: sqlite3.Connection, error: ApiError) -> None:
    if not connection.in_transaction:
        return
    if error.code == "identity.mfa_verification_failed":
        connection.commit()
    else:
        connection.rollback()


def _finish_support_error(connection: sqlite3.Connection, error: ApiError) -> None:
    if not connection.in_transaction:
        return
    if error.code in {"support.session_expired", "support.session_invalid"}:
        connection.commit()
    else:
        connection.rollback()


def _rollback(connection: sqlite3.Connection) -> None:
    if connection.in_transaction:
        connection.rollback()


def _id(prefix: str) -> str:
    return prefix + uuid4().hex


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _db_time(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
