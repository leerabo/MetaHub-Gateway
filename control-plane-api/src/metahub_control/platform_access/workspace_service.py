"""PRD-096 cross-Workspace directory and lifecycle operations."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import re
import sqlite3
from datetime import datetime, timedelta
from typing import Literal
from uuid import uuid4

from metahub_shared.db.connection import connect_database
from metahub_shared.db.errors import DbError
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError

from .models import PlatformResponseModel
from .service import (
    PlatformContext,
    _audit,
    _authenticate_platform,
    _db_time,
    _finish_auth_error,
    _parse_time,
    _require_permission,
    _rollback,
    _utcnow,
)
from .workspace_models import (
    OwnerNotificationResponse,
    PlatformActionSummary,
    PlatformWorkspaceDetailResponse,
    PlatformWorkspaceHealthResponse,
    PlatformWorkspaceListResponse,
    PlatformWorkspaceMutationResponse,
    PlatformWorkspaceSummary,
    WorkspaceHealthSummary,
    WorkspaceOperationalEventListResponse,
    WorkspaceOperationalEventSummary,
)

_WORKSPACE_ID_RE = re.compile(r"^wsp_[0-9a-f]{32}$")
_SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{1,46}[a-z0-9])$")
_CURSOR_DOMAIN = b"prd096/platform/workspace-directory-cursor/v1"
_IDEMPOTENCY_DOMAIN = b"prd096/platform/workspace-idempotency/v1"
_READ_PERMISSIONS = frozenset(
    {
        "platform.workspace.directory.read",
        "platform.billing.read",
        "platform.customer_success.read",
        "platform.workspace.risk.manage",
    }
)
_SECURITY_TAGS = frozenset({"security_review", "compliance_hold"})
_CUSTOMER_SUCCESS_TAGS = frozenset({"payment_attention", "adoption_attention"})
_ALL_RISK_TAGS = _SECURITY_TAGS | _CUSTOMER_SUCCESS_TAGS
_SOURCE_HEALTH_TAGS = (
    "usage_declining",
    "high_error_rate",
    "cost_anomaly_open",
    "budget_missing",
    "team_inactive",
)
_SUSPEND_REASONS = frozenset(
    {"security_containment", "terms_enforcement", "operational_protection"}
)
_RESTORE_REASONS = frozenset({"security_cleared", "terms_resolved", "operational_recovery"})
_RISK_REASONS = frozenset(
    {"security_review", "compliance_review", "payment_follow_up", "adoption_follow_up"}
)
_NOTIFICATION_REASONS = frozenset({"operational_notice", "risk_follow_up", "service_update"})
_NOTIFICATION_TEMPLATES = frozenset(
    {"workspace_operational_notice", "workspace_risk_follow_up", "workspace_service_update"}
)
_SUMMARY_SELECT = """
SELECT state.workspace_id,state.tenant_id,state.status operational_status,
  state.revision operational_revision,state.risk_revision,state.updated_at,
  workspace.kind,workspace.created_at,
  COALESCE(organization.name,personal.display_label) display_name,
  organization.slug_normalized,region.region_code,region.residency_policy_version,
  data_state.lifecycle_status,
  customer_account.customer_account_id health_customer_account_id,
  health.customer_health_snapshot_id source_score_id,health.source_sequence,
  health.risk_level score_band,health.risk_level health_risk_level,
  health.projection_sha256 source_projection_sha256,
  health_snapshot.evaluated_at source_evaluated_at,
  (SELECT COUNT(*) FROM workspace_memberships member
    WHERE member.workspace_id=state.workspace_id AND member.status='active') member_count,
  (SELECT COUNT(*) FROM workspace_sessions session
    WHERE session.workspace_id=state.workspace_id AND session.status='active') active_session_count,
  (SELECT export.status FROM tenant_export_jobs export
    WHERE export.tenant_id=state.tenant_id
    ORDER BY export.created_at DESC,export.tenant_export_job_id DESC LIMIT 1) latest_export_status,
  (SELECT deletion.status FROM deletion_requests deletion
    WHERE deletion.tenant_id=state.tenant_id
    ORDER BY deletion.created_at DESC,deletion.deletion_request_id DESC LIMIT 1)
    latest_deletion_status,
  CASE WHEN (SELECT COUNT(*) FROM workspace_memberships owner_member
      JOIN global_accounts owner_account ON owner_account.account_id=owner_member.account_id
      WHERE owner_member.workspace_id=state.workspace_id
        AND owner_member.role_code='owner' AND owner_member.status='active'
        AND owner_account.status='active' AND owner_account.login_enabled=1)=1
    THEN 'available' ELSE 'unavailable' END owner_contact_status,
  (SELECT notification.status FROM workspace_owner_notification_outbox notification
    WHERE notification.workspace_id=state.workspace_id
    ORDER BY notification.created_at DESC,notification.workspace_owner_notification_id DESC LIMIT 1)
    last_notification_status,
  COALESCE((SELECT json_group_array(active_tag.tag_code) FROM(
    SELECT tag.tag_code FROM workspace_risk_tags tag
    WHERE tag.workspace_id=state.workspace_id AND tag.status='active'
    ORDER BY tag.tag_code) active_tag),'[]') platform_risk_tags_json
FROM workspace_operational_states state
JOIN workspaces workspace ON workspace.workspace_id=state.workspace_id
JOIN workspace_tenant_bindings binding
  ON binding.workspace_id=state.workspace_id AND binding.tenant_id=state.tenant_id
LEFT JOIN workspace_regions region ON region.workspace_id=state.workspace_id
LEFT JOIN organization_profiles organization ON organization.workspace_id=state.workspace_id
LEFT JOIN personal_profiles personal ON personal.workspace_id=state.workspace_id
LEFT JOIN tenant_data_states data_state ON data_state.tenant_id=state.tenant_id
LEFT JOIN platform_customer_account_projections customer_account
  ON customer_account.workspace_id=state.workspace_id
 AND customer_account.tenant_id=state.tenant_id
LEFT JOIN platform_customer_health_heads health
  ON health.customer_account_id=customer_account.customer_account_id
 AND health.workspace_id=state.workspace_id AND health.tenant_id=state.tenant_id
LEFT JOIN platform_customer_health_snapshots health_snapshot
  ON health_snapshot.customer_health_snapshot_id=health.customer_health_snapshot_id
 AND health_snapshot.customer_account_id=health.customer_account_id
 AND health_snapshot.workspace_id=health.workspace_id
 AND health_snapshot.tenant_id=health.tenant_id
"""


def authorize_workspace_write(
    config: AppConfig,
    *,
    session_token: str,
    csrf_token: str,
    capability: Literal["lifecycle", "risk", "contact"],
) -> None:
    connection = _connect(config)
    try:
        connection.execute("BEGIN")
        context = _authenticate_platform(
            connection, config, session_token, csrf_token=csrf_token, touch=False
        )
        _require_capability(context, capability)
        connection.commit()
    except ApiError as exc:
        _finish_auth_error(connection, exc)
        raise
    except (sqlite3.Error, ValueError, TypeError):
        _rollback(connection)
        raise ApiError("platform.workspace_unavailable") from None
    finally:
        connection.close()


def list_platform_workspaces(
    config: AppConfig,
    *,
    session_token: str,
    limit: int,
    cursor: str | None,
    filters: dict[str, str],
    request_id: str,
    trace_id: str,
) -> PlatformWorkspaceListResponse:
    connection = _connect(config)
    try:
        connection.execute("BEGIN")
        context = _authenticate_platform(connection, config, session_token, touch=True)
        _require_directory(context)
        normalized = _normalize_filters(filters)
        filter_digest = _filter_digest(normalized)
        boundary = (
            _decode_cursor(config, context, cursor, filter_digest) if cursor is not None else None
        )
        predicates: list[str] = []
        parameters: list[object] = []
        _append_filters(predicates, parameters, normalized)
        if boundary is not None:
            predicates.append(
                "(state.updated_at<? OR (state.updated_at=? AND state.workspace_id>?))"
            )
            parameters.extend((boundary[0], boundary[0], boundary[1]))
        where = " AND ".join(predicates) if predicates else "1=1"
        rows = connection.execute(
            _SUMMARY_SELECT
            + f" WHERE {where} ORDER BY state.updated_at DESC,state.workspace_id LIMIT ?",
            (*parameters, limit + 1),
        ).fetchall()
        visible = rows[:limit]
        has_more = len(rows) > limit
        next_cursor = None
        if has_more:
            last = visible[-1]
            next_cursor = _encode_cursor(
                config,
                context,
                filter_digest,
                updated_at=last["updated_at"],
                workspace_id=last["workspace_id"],
            )
        now = _utcnow()
        response = PlatformWorkspaceListResponse(
            items=tuple(_summary(connection, row, context, now) for row in visible),
            limit=limit,
            has_more=has_more,
            next_cursor=next_cursor,
            request_id=request_id,
            trace_id=trace_id,
        )
        connection.commit()
        return response
    except ApiError as exc:
        _finish_workspace_error(connection, exc)
        raise
    except (sqlite3.Error, ValueError, TypeError, json.JSONDecodeError):
        _rollback(connection)
        raise ApiError("platform.workspace_unavailable") from None
    finally:
        connection.close()


def get_platform_workspace(
    config: AppConfig,
    *,
    session_token: str,
    workspace_id: str,
    request_id: str,
    trace_id: str,
) -> PlatformWorkspaceDetailResponse:
    connection = _connect(config)
    try:
        connection.execute("BEGIN")
        context = _authenticate_platform(connection, config, session_token, touch=True)
        _require_directory(context)
        row = _workspace_row(connection, workspace_id)
        actions = connection.execute(
            """
            SELECT platform_action_intent_id,action_type,reason_code,result_revision,created_at
            FROM platform_action_intents WHERE workspace_id=?
            ORDER BY created_at DESC,rowid DESC LIMIT 20
            """,
            (row["workspace_id"],),
        ).fetchall()
        response = PlatformWorkspaceDetailResponse(
            workspace=_summary(connection, row, context, _utcnow()),
            recent_actions=tuple(
                PlatformActionSummary(
                    action_intent_id=item["platform_action_intent_id"],
                    action_type=item["action_type"],
                    reason_code=item["reason_code"],
                    result_revision=int(item["result_revision"]),
                    created_at=_parse_time(item["created_at"]),
                )
                for item in actions
            ),
            request_id=request_id,
            trace_id=trace_id,
        )
        connection.commit()
        return response
    except ApiError as exc:
        _finish_workspace_error(connection, exc)
        raise
    except (sqlite3.Error, ValueError, TypeError, json.JSONDecodeError):
        _rollback(connection)
        raise ApiError("platform.workspace_unavailable") from None
    finally:
        connection.close()


def get_platform_workspace_health(
    config: AppConfig,
    *,
    session_token: str,
    workspace_id: str,
    request_id: str,
    trace_id: str,
) -> PlatformWorkspaceHealthResponse:
    connection = _connect(config)
    try:
        connection.execute("BEGIN")
        context = _authenticate_platform(connection, config, session_token, touch=True)
        _require_directory(context)
        row = _workspace_row(connection, workspace_id)
        response = PlatformWorkspaceHealthResponse(
            workspace_id=row["workspace_id"],
            health=_health(connection, row, context, _utcnow()),
            request_id=request_id,
            trace_id=trace_id,
        )
        connection.commit()
        return response
    except ApiError as exc:
        _finish_workspace_error(connection, exc)
        raise
    except (sqlite3.Error, ValueError, TypeError, json.JSONDecodeError):
        _rollback(connection)
        raise ApiError("platform.workspace_unavailable") from None
    finally:
        connection.close()


def suspend_workspace(
    config: AppConfig,
    *,
    session_token: str,
    csrf_token: str,
    workspace_id: str,
    expected_revision: int,
    idempotency_key: str,
    reason_code: str,
    request_id: str,
    trace_id: str,
) -> PlatformWorkspaceMutationResponse:
    if reason_code not in _SUSPEND_REASONS:
        raise ApiError("platform.request_invalid")
    return _change_lifecycle(
        config,
        session_token=session_token,
        csrf_token=csrf_token,
        workspace_id=workspace_id,
        expected_revision=expected_revision,
        idempotency_key=idempotency_key,
        action_type="suspend",
        reason_code=reason_code,
        request_id=request_id,
        trace_id=trace_id,
    )


def restore_workspace(
    config: AppConfig,
    *,
    session_token: str,
    csrf_token: str,
    workspace_id: str,
    expected_revision: int,
    idempotency_key: str,
    reason_code: str,
    request_id: str,
    trace_id: str,
) -> PlatformWorkspaceMutationResponse:
    if reason_code not in _RESTORE_REASONS:
        raise ApiError("platform.request_invalid")
    return _change_lifecycle(
        config,
        session_token=session_token,
        csrf_token=csrf_token,
        workspace_id=workspace_id,
        expected_revision=expected_revision,
        idempotency_key=idempotency_key,
        action_type="restore",
        reason_code=reason_code,
        request_id=request_id,
        trace_id=trace_id,
    )


def add_workspace_risk_tag(
    config: AppConfig,
    *,
    session_token: str,
    csrf_token: str,
    workspace_id: str,
    expected_revision: int,
    idempotency_key: str,
    tag_code: str,
    reason_code: str,
    request_id: str,
    trace_id: str,
) -> PlatformWorkspaceMutationResponse:
    return _change_risk_tag(
        config,
        session_token=session_token,
        csrf_token=csrf_token,
        workspace_id=workspace_id,
        expected_revision=expected_revision,
        idempotency_key=idempotency_key,
        tag_code=tag_code,
        reason_code=reason_code,
        action_type="risk_tag_add",
        request_id=request_id,
        trace_id=trace_id,
    )


def remove_workspace_risk_tag(
    config: AppConfig,
    *,
    session_token: str,
    csrf_token: str,
    workspace_id: str,
    expected_revision: int,
    idempotency_key: str,
    tag_code: str,
    reason_code: str,
    request_id: str,
    trace_id: str,
) -> PlatformWorkspaceMutationResponse:
    return _change_risk_tag(
        config,
        session_token=session_token,
        csrf_token=csrf_token,
        workspace_id=workspace_id,
        expected_revision=expected_revision,
        idempotency_key=idempotency_key,
        tag_code=tag_code,
        reason_code=reason_code,
        action_type="risk_tag_remove",
        request_id=request_id,
        trace_id=trace_id,
    )


def notify_workspace_owner(
    config: AppConfig,
    *,
    session_token: str,
    csrf_token: str,
    workspace_id: str,
    expected_revision: int,
    idempotency_key: str,
    template_code: str,
    reason_code: str,
    request_id: str,
    trace_id: str,
) -> OwnerNotificationResponse:
    if template_code not in _NOTIFICATION_TEMPLATES or reason_code not in _NOTIFICATION_REASONS:
        raise ApiError("platform.request_invalid")
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        context = _authenticate_platform(
            connection, config, session_token, csrf_token=csrf_token, touch=True
        )
        _require_capability(context, "contact")
        request_hash = _request_hash(
            "owner_notification",
            workspace_id,
            expected_revision,
            {"reason_code": reason_code, "template_code": template_code},
        )
        replay = _replay(
            connection,
            config,
            context,
            idempotency_key,
            request_hash,
            OwnerNotificationResponse,
        )
        if replay is not None:
            connection.commit()
            return replay
        state = _state_row(connection, workspace_id)
        if int(state["revision"]) != expected_revision:
            raise ApiError("platform.workspace_revision_conflict")
        if state["status"] in {"deleted", "migration_conflict"}:
            raise ApiError("platform.workspace_lifecycle_conflict")
        owners = connection.execute(
            """
            SELECT member.workspace_membership_id,member.account_id
            FROM workspace_memberships member
            JOIN global_accounts account ON account.account_id=member.account_id
            WHERE member.workspace_id=? AND member.tenant_id=?
              AND member.role_code='owner' AND member.status='active'
              AND account.status='active' AND account.login_enabled=1
            """,
            (state["workspace_id"], state["tenant_id"]),
        ).fetchall()
        if len(owners) != 1:
            raise ApiError("platform.workspace_notification_unavailable")
        now = _utcnow()
        intent_id = _id("pai_")
        response = OwnerNotificationResponse(
            action_intent_id=intent_id,
            workspace_id=state["workspace_id"],
            notification_status="pending",
            request_id=request_id,
            trace_id=trace_id,
        )
        _insert_intent(
            connection,
            config,
            context,
            intent_id=intent_id,
            workspace_id=state["workspace_id"],
            tenant_id=state["tenant_id"],
            action_type="owner_notification",
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            expected_revision=expected_revision,
            result_revision=int(state["revision"]),
            reason_code=reason_code,
            response=response,
            request_id=request_id,
            trace_id=trace_id,
            now=now,
        )
        payload = json.dumps(
            {
                "action_intent_id": intent_id,
                "schema_version": 1,
                "template_code": template_code,
            },
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        connection.execute(
            """
            INSERT INTO workspace_owner_notification_outbox(
              workspace_owner_notification_id,platform_action_intent_id,workspace_id,
              tenant_id,owner_membership_id,owner_account_id,template_code,payload_json,
              payload_sha256,status,attempt_count,next_attempt_at,last_error_code,sent_at,
              created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,'pending',0,?,NULL,NULL,?,?)
            """,
            (
                _id("wno_"),
                intent_id,
                state["workspace_id"],
                state["tenant_id"],
                owners[0]["workspace_membership_id"],
                owners[0]["account_id"],
                template_code,
                payload,
                hashlib.sha256(payload.encode("ascii")).hexdigest(),
                _db_time(now),
                _db_time(now),
                _db_time(now),
            ),
        )
        _write_event(
            connection,
            state,
            context,
            intent_id,
            "workspace.owner_notification_requested",
            reason_code,
            request_id,
            trace_id,
            now,
        )
        _audit_workspace_action(
            connection,
            context,
            "workspace.owner_notification_requested",
            state["workspace_id"],
            reason_code,
            request_id,
            trace_id,
            now,
            {"template_code": template_code},
        )
        connection.commit()
        return response
    except ApiError as exc:
        _finish_workspace_error(connection, exc)
        raise
    except (sqlite3.Error, ValueError, TypeError, json.JSONDecodeError):
        _rollback(connection)
        raise ApiError("platform.workspace_unavailable") from None
    finally:
        connection.close()


def list_workspace_events(
    config: AppConfig,
    *,
    session_token: str,
    workspace_id: str,
    limit: int,
    request_id: str,
    trace_id: str,
) -> WorkspaceOperationalEventListResponse:
    connection = _connect(config)
    try:
        connection.execute("BEGIN")
        context = _authenticate_platform(connection, config, session_token, touch=True)
        _require_directory(context)
        state = _state_row(connection, workspace_id)
        rows = connection.execute(
            """
            SELECT workspace_operational_event_id,event_type,reason_code,
              operational_revision,risk_revision,occurred_at
            FROM workspace_operational_events WHERE workspace_id=?
            ORDER BY occurred_at DESC,rowid DESC LIMIT ?
            """,
            (state["workspace_id"], limit + 1),
        ).fetchall()
        response = WorkspaceOperationalEventListResponse(
            events=tuple(
                WorkspaceOperationalEventSummary(
                    event_id=row["workspace_operational_event_id"],
                    event_type=row["event_type"],
                    reason_code=row["reason_code"],
                    operational_revision=int(row["operational_revision"]),
                    risk_revision=int(row["risk_revision"]),
                    occurred_at=_parse_time(row["occurred_at"]),
                )
                for row in rows[:limit]
            ),
            limit=limit,
            has_more=len(rows) > limit,
            request_id=request_id,
            trace_id=trace_id,
        )
        connection.commit()
        return response
    except ApiError as exc:
        _finish_workspace_error(connection, exc)
        raise
    except (sqlite3.Error, ValueError, TypeError):
        _rollback(connection)
        raise ApiError("platform.workspace_unavailable") from None
    finally:
        connection.close()


def _change_lifecycle(
    config: AppConfig,
    *,
    session_token: str,
    csrf_token: str,
    workspace_id: str,
    expected_revision: int,
    idempotency_key: str,
    action_type: Literal["suspend", "restore"],
    reason_code: str,
    request_id: str,
    trace_id: str,
) -> PlatformWorkspaceMutationResponse:
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        context = _authenticate_platform(
            connection, config, session_token, csrf_token=csrf_token, touch=True
        )
        _require_capability(context, "lifecycle")
        request_hash = _request_hash(
            action_type, workspace_id, expected_revision, {"reason_code": reason_code}
        )
        replay = _replay(
            connection,
            config,
            context,
            idempotency_key,
            request_hash,
            PlatformWorkspaceMutationResponse,
        )
        if replay is not None:
            connection.commit()
            return replay
        state = _state_row(connection, workspace_id)
        if int(state["revision"]) != expected_revision:
            raise ApiError("platform.workspace_revision_conflict")
        now = _utcnow()
        stamp = _db_time(now)
        if action_type == "suspend":
            if (
                state["status"] != "active"
                or state["workspace_status"] != "active"
                or state["binding_status"] != "active"
                or state["tenant_status"] != "active"
                or state["lifecycle_status"] != "active"
            ):
                raise ApiError("platform.workspace_lifecycle_conflict")
            updated = connection.execute(
                "UPDATE tenants SET status='disabled',updated_at=? "
                "WHERE tenant_id=? AND status='active'",
                (stamp, state["tenant_id"]),
            ).rowcount
            if updated != 1:
                raise ApiError("platform.workspace_lifecycle_conflict")
            updated = connection.execute(
                """
                UPDATE workspace_operational_states
                SET status='suspended',revision=revision+1,source='platform_suspend',
                  suspended_reason_code=?,suspended_by_operator_id=?,suspended_at=?,updated_at=?
                WHERE workspace_id=? AND revision=? AND status='active'
                """,
                (
                    reason_code,
                    context.operator_id,
                    stamp,
                    stamp,
                    state["workspace_id"],
                    expected_revision,
                ),
            ).rowcount
            event_type = "workspace.suspended"
            revoked_reason = "platform_suspended"
        else:
            if not _restore_allowed(connection, state):
                raise ApiError("platform.workspace_lifecycle_conflict")
            updated = connection.execute(
                "UPDATE tenants SET status='active',updated_at=? "
                "WHERE tenant_id=? AND status='disabled'",
                (stamp, state["tenant_id"]),
            ).rowcount
            if updated != 1:
                raise ApiError("platform.workspace_lifecycle_conflict")
            updated = connection.execute(
                """
                UPDATE workspace_operational_states
                SET status='active',revision=revision+1,source='platform_restore',
                  suspended_reason_code=NULL,suspended_by_operator_id=NULL,
                  suspended_at=NULL,updated_at=?
                WHERE workspace_id=? AND revision=? AND status='suspended'
                """,
                (stamp, state["workspace_id"], expected_revision),
            ).rowcount
            event_type = "workspace.restored"
            revoked_reason = "platform_restore_requires_reauthentication"
        if updated != 1:
            raise ApiError("platform.workspace_revision_conflict")
        connection.execute(
            "UPDATE workspaces SET revision=revision+1,updated_at=? WHERE workspace_id=?",
            (stamp, state["workspace_id"]),
        )
        connection.execute(
            """
            UPDATE workspace_sessions
            SET status='revoked',revoked_at=?,revoked_reason=?,updated_at=?
            WHERE workspace_id=? AND status='active'
            """,
            (stamp, revoked_reason, stamp, state["workspace_id"]),
        )
        current = _workspace_row(connection, state["workspace_id"])
        intent_id = _id("pai_")
        response = PlatformWorkspaceMutationResponse(
            action_intent_id=intent_id,
            workspace=_summary(connection, current, context, now),
            request_id=request_id,
            trace_id=trace_id,
        )
        _insert_intent(
            connection,
            config,
            context,
            intent_id=intent_id,
            workspace_id=state["workspace_id"],
            tenant_id=state["tenant_id"],
            action_type=action_type,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            expected_revision=expected_revision,
            result_revision=int(current["operational_revision"]),
            reason_code=reason_code,
            response=response,
            request_id=request_id,
            trace_id=trace_id,
            now=now,
        )
        _write_event(
            connection,
            current,
            context,
            intent_id,
            event_type,
            reason_code,
            request_id,
            trace_id,
            now,
        )
        _audit_workspace_action(
            connection,
            context,
            event_type,
            state["workspace_id"],
            reason_code,
            request_id,
            trace_id,
            now,
            {
                "operational_revision": int(current["operational_revision"]),
                "operational_status": current["operational_status"],
            },
        )
        connection.commit()
        return response
    except ApiError as exc:
        _finish_workspace_error(connection, exc)
        raise
    except (sqlite3.Error, ValueError, TypeError, json.JSONDecodeError):
        _rollback(connection)
        raise ApiError("platform.workspace_unavailable") from None
    finally:
        connection.close()


def _change_risk_tag(
    config: AppConfig,
    *,
    session_token: str,
    csrf_token: str,
    workspace_id: str,
    expected_revision: int,
    idempotency_key: str,
    tag_code: str,
    reason_code: str,
    action_type: Literal["risk_tag_add", "risk_tag_remove"],
    request_id: str,
    trace_id: str,
) -> PlatformWorkspaceMutationResponse:
    if tag_code not in _ALL_RISK_TAGS or reason_code not in _RISK_REASONS:
        raise ApiError("platform.request_invalid")
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        context = _authenticate_platform(
            connection, config, session_token, csrf_token=csrf_token, touch=True
        )
        _require_capability(context, "risk")
        if tag_code not in _allowed_risk_tags(context):
            raise ApiError("platform.permission_denied")
        request_hash = _request_hash(
            action_type,
            workspace_id,
            expected_revision,
            {"reason_code": reason_code, "tag_code": tag_code},
        )
        replay = _replay(
            connection,
            config,
            context,
            idempotency_key,
            request_hash,
            PlatformWorkspaceMutationResponse,
        )
        if replay is not None:
            connection.commit()
            return replay
        state = _state_row(connection, workspace_id)
        if int(state["risk_revision"]) != expected_revision:
            raise ApiError("platform.workspace_risk_conflict")
        if state["status"] in {"deleted", "migration_conflict"}:
            raise ApiError("platform.workspace_lifecycle_conflict")
        existing = connection.execute(
            "SELECT * FROM workspace_risk_tags WHERE workspace_id=? AND tag_code=?",
            (state["workspace_id"], tag_code),
        ).fetchone()
        now = _utcnow()
        stamp = _db_time(now)
        if action_type == "risk_tag_add":
            if existing is not None and existing["status"] == "active":
                raise ApiError("platform.workspace_risk_conflict")
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO workspace_risk_tags(
                      workspace_risk_tag_id,workspace_id,tenant_id,tag_code,status,revision,
                      created_by_operator_id,updated_by_operator_id,revoked_by_operator_id,
                      created_at,updated_at,revoked_at)
                    VALUES(?,?,?,?,'active',1,?,?,NULL,?,?,NULL)
                    """,
                    (
                        _id("wrt_"),
                        state["workspace_id"],
                        state["tenant_id"],
                        tag_code,
                        context.operator_id,
                        context.operator_id,
                        stamp,
                        stamp,
                    ),
                )
            else:
                connection.execute(
                    """
                    UPDATE workspace_risk_tags SET status='active',revision=revision+1,
                      updated_by_operator_id=?,revoked_by_operator_id=NULL,
                      revoked_at=NULL,updated_at=? WHERE workspace_risk_tag_id=?
                    """,
                    (context.operator_id, stamp, existing["workspace_risk_tag_id"]),
                )
            event_type = "workspace.risk_tag_added"
        else:
            if existing is None or existing["status"] != "active":
                raise ApiError("platform.workspace_risk_conflict")
            connection.execute(
                """
                UPDATE workspace_risk_tags SET status='revoked',revision=revision+1,
                  updated_by_operator_id=?,revoked_by_operator_id=?,revoked_at=?,updated_at=?
                WHERE workspace_risk_tag_id=? AND status='active'
                """,
                (
                    context.operator_id,
                    context.operator_id,
                    stamp,
                    stamp,
                    existing["workspace_risk_tag_id"],
                ),
            )
            event_type = "workspace.risk_tag_removed"
        updated = connection.execute(
            """
            UPDATE workspace_operational_states SET risk_revision=risk_revision+1,updated_at=?
            WHERE workspace_id=? AND risk_revision=?
            """,
            (stamp, state["workspace_id"], expected_revision),
        ).rowcount
        if updated != 1:
            raise ApiError("platform.workspace_risk_conflict")
        current = _workspace_row(connection, state["workspace_id"])
        intent_id = _id("pai_")
        response = PlatformWorkspaceMutationResponse(
            action_intent_id=intent_id,
            workspace=_summary(connection, current, context, now),
            request_id=request_id,
            trace_id=trace_id,
        )
        _insert_intent(
            connection,
            config,
            context,
            intent_id=intent_id,
            workspace_id=state["workspace_id"],
            tenant_id=state["tenant_id"],
            action_type=action_type,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            expected_revision=expected_revision,
            result_revision=int(current["risk_revision"]),
            reason_code=reason_code,
            response=response,
            request_id=request_id,
            trace_id=trace_id,
            now=now,
        )
        _write_event(
            connection,
            current,
            context,
            intent_id,
            event_type,
            reason_code,
            request_id,
            trace_id,
            now,
        )
        _audit_workspace_action(
            connection,
            context,
            event_type,
            state["workspace_id"],
            reason_code,
            request_id,
            trace_id,
            now,
            {"risk_revision": int(current["risk_revision"]), "tag_code": tag_code},
        )
        connection.commit()
        return response
    except ApiError as exc:
        _finish_workspace_error(connection, exc)
        raise
    except (sqlite3.Error, ValueError, TypeError, json.JSONDecodeError):
        _rollback(connection)
        raise ApiError("platform.workspace_unavailable") from None
    finally:
        connection.close()


def _restore_allowed(connection: sqlite3.Connection, state: sqlite3.Row) -> bool:
    if (
        state["status"] != "suspended"
        or state["source"] != "platform_suspend"
        or state["workspace_status"] != "active"
        or state["binding_status"] != "active"
        or state["tenant_status"] != "disabled"
        or state["lifecycle_status"] != "active"
        or state["active_deletion_request_id"] is not None
    ):
        return False
    if (
        connection.execute(
            """
        SELECT COUNT(*) FROM workspace_memberships member
        JOIN global_accounts account ON account.account_id=member.account_id
        WHERE member.workspace_id=? AND member.tenant_id=?
          AND member.role_code='owner' AND member.status='active'
          AND account.status='active' AND account.login_enabled=1
        """,
            (state["workspace_id"], state["tenant_id"]),
        ).fetchone()[0]
        != 1
    ):
        return False
    if (
        connection.execute(
            """
        SELECT 1 FROM deletion_requests WHERE tenant_id=?
          AND (status IN('requested','quiescing','deleting','completed')
            OR lease_owner_id IS NOT NULL OR lease_token IS NOT NULL
            OR lease_expires_at IS NOT NULL) LIMIT 1
        """,
            (state["tenant_id"],),
        ).fetchone()
        is not None
    ):
        return False
    proof = connection.execute(
        """
        SELECT 1 FROM platform_action_intents intent
        JOIN platform_audit_logs audit
          ON audit.action_code='workspace.suspended'
         AND audit.object_type='workspace' AND audit.object_id=intent.workspace_id
         AND audit.actor_id=intent.operator_id
         AND audit.request_id=intent.request_id AND audit.trace_id=intent.trace_id
        WHERE intent.workspace_id=? AND intent.action_type='suspend'
          AND intent.operator_id=? AND intent.result_revision=?
          AND intent.created_at=? AND intent.status='completed'
        ORDER BY intent.created_at DESC LIMIT 1
        """,
        (
            state["workspace_id"],
            state["suspended_by_operator_id"],
            state["revision"],
            state["suspended_at"],
        ),
    ).fetchone()
    return proof is not None


def _workspace_row(connection: sqlite3.Connection, workspace_id: str) -> sqlite3.Row:
    if _WORKSPACE_ID_RE.fullmatch(workspace_id) is None:
        raise ApiError("platform.workspace_not_found")
    row = connection.execute(
        _SUMMARY_SELECT + " WHERE state.workspace_id=?",
        (workspace_id,),
    ).fetchone()
    if row is None:
        raise ApiError("platform.workspace_not_found")
    return row


def _state_row(connection: sqlite3.Connection, workspace_id: str) -> sqlite3.Row:
    if _WORKSPACE_ID_RE.fullmatch(workspace_id) is None:
        raise ApiError("platform.workspace_not_found")
    row = connection.execute(
        """
        SELECT state.*,workspace.status workspace_status,binding.status binding_status,
          tenant.status tenant_status,data_state.lifecycle_status,
          data_state.active_deletion_request_id
        FROM workspace_operational_states state
        JOIN workspaces workspace ON workspace.workspace_id=state.workspace_id
        JOIN workspace_tenant_bindings binding
          ON binding.workspace_id=state.workspace_id AND binding.tenant_id=state.tenant_id
        JOIN tenants tenant ON tenant.tenant_id=state.tenant_id
        JOIN tenant_data_states data_state ON data_state.tenant_id=state.tenant_id
        WHERE state.workspace_id=?
        """,
        (workspace_id,),
    ).fetchone()
    if row is None:
        raise ApiError("platform.workspace_not_found")
    return row


def _summary(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    context: PlatformContext,
    now: datetime,
) -> PlatformWorkspaceSummary:
    permissions = set(context.permissions)
    can_view_risk = bool(
        permissions & {"platform.workspace.risk.manage", "platform.customer_success.read"}
    )
    can_view_contact = bool(
        permissions & {"platform.workspace.lifecycle.manage", "platform.customer_success.read"}
    )
    lifecycle = row["lifecycle_status"]
    if lifecycle not in {"active", "deletion_requested", "deleted"}:
        lifecycle = "conflict"
    return PlatformWorkspaceSummary(
        workspace_id=row["workspace_id"],
        kind=row["kind"],
        display_name=row["display_name"] or "Unavailable",
        slug=row["slug_normalized"],
        created_at=_parse_time(row["created_at"]),
        operational_status=row["operational_status"],
        operational_revision=int(row["operational_revision"]),
        risk_revision=int(row["risk_revision"]),
        region_code=row["region_code"],
        residency_policy_version=row["residency_policy_version"],
        member_count=int(row["member_count"]),
        active_session_count=int(row["active_session_count"]),
        plan_status="not_available" if "platform.billing.read" in permissions else None,
        deployment_status="not_configured",
        data_lifecycle_status=lifecycle,
        latest_export_status=row["latest_export_status"],
        latest_deletion_status=row["latest_deletion_status"],
        gateway_status=(
            "transition_pending" if row["operational_status"] == "suspended" else "not_applicable"
        ),
        health=_health(connection, row, context, now),
        platform_risk_tags=(
            tuple(json.loads(row["platform_risk_tags_json"])) if can_view_risk else None
        ),
        owner_contact_status=row["owner_contact_status"] if can_view_contact else None,
        last_notification_status=(row["last_notification_status"] if can_view_contact else None),
        updated_at=_parse_time(row["updated_at"]),
    )


def _health(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    context: PlatformContext,
    now: datetime,
) -> WorkspaceHealthSummary:
    if row["source_score_id"] is None:
        return WorkspaceHealthSummary(status="not_ready")
    source = connection.execute(
        """SELECT * FROM platform_customer_health_snapshots
            WHERE customer_account_id=? AND workspace_id=? AND tenant_id=?
              AND customer_health_snapshot_id=?""",
        (
            row["health_customer_account_id"],
            row["workspace_id"],
            row["tenant_id"],
            row["source_score_id"],
        ),
    ).fetchone()
    try:
        if source is None:
            raise ValueError
        source_tags = tuple(
            code
            for code in _SOURCE_HEALTH_TAGS
            if int(source[code]) == 1
        )
        evaluated = _parse_time(source["evaluated_at"])
        if (
            int(row["source_sequence"]) != int(source["customer_health_snapshot_sequence"])
            or row["source_projection_sha256"] != source["health_projection_sha256"]
            or row["score_band"] != source["risk_level"]
            or row["health_risk_level"] != source["risk_level"]
            or evaluated > now
        ):
            raise ValueError
    except (ApiError, ValueError, TypeError, json.JSONDecodeError):
        return WorkspaceHealthSummary(status="unavailable")
    can_view_tags = bool(
        set(context.permissions)
        & {"platform.workspace.risk.manage", "platform.customer_success.read"}
    )
    return WorkspaceHealthSummary(
        status="stale" if evaluated < now - timedelta(hours=2) else "fresh",
        score_band=source["risk_level"],
        risk_level=source["risk_level"],
        risk_tags=source_tags if can_view_tags else None,
        evaluated_at=evaluated,
    )


def _normalize_filters(filters: dict[str, str]) -> dict[str, str]:
    allowed_values = {
        "kind": {"personal", "organization"},
        "operational_status": {
            "active",
            "suspended",
            "deletion_pending",
            "deleted",
            "migration_conflict",
        },
        "health_status": {"fresh", "stale", "not_ready", "unavailable"},
        "health_risk": {"healthy", "watch", "at_risk", "critical"},
        "plan_status": {"not_available"},
        "deployment_status": {"not_configured"},
        "risk_tag": _ALL_RISK_TAGS,
    }
    normalized: dict[str, str] = {}
    for key, value in filters.items():
        text = str(value)
        if not text or len(text.encode("utf-8")) > 80:
            raise ApiError("platform.request_invalid")
        if key == "search":
            if _WORKSPACE_ID_RE.fullmatch(text) is None and _SLUG_RE.fullmatch(text) is None:
                raise ApiError("platform.request_invalid")
        elif key == "region":
            if re.fullmatch(r"[a-z0-9][a-z0-9-]{1,38}[a-z0-9]", text) is None:
                raise ApiError("platform.request_invalid")
        elif key not in allowed_values or text not in allowed_values[key]:
            raise ApiError("platform.request_invalid")
        normalized[key] = text
    return dict(sorted(normalized.items()))


def _append_filters(
    predicates: list[str], parameters: list[object], filters: dict[str, str]
) -> None:
    search = filters.get("search")
    if search is not None:
        if _WORKSPACE_ID_RE.fullmatch(search):
            predicates.append("state.workspace_id=?")
        else:
            predicates.append("organization.slug_normalized=?")
        parameters.append(search)
    mapping = {
        "kind": "workspace.kind=?",
        "operational_status": "state.status=?",
        "region": "region.region_code=?",
        "health_risk": "health.risk_level=?",
    }
    for key, predicate in mapping.items():
        if key in filters:
            predicates.append(predicate)
            parameters.append(filters[key])
    if "risk_tag" in filters:
        predicates.append(
            "EXISTS(SELECT 1 FROM workspace_risk_tags filter_tag "
            "WHERE filter_tag.workspace_id=state.workspace_id "
            "AND filter_tag.status='active' AND filter_tag.tag_code=?)"
        )
        parameters.append(filters["risk_tag"])
    if "health_status" in filters:
        now = _db_time(_utcnow())
        predicates.append(
            "CASE WHEN health.customer_account_id IS NULL THEN 'not_ready' "
            "WHEN unixepoch(health_snapshot.evaluated_at)>unixepoch(?) THEN 'unavailable' "
            "WHEN unixepoch(?)-unixepoch(health_snapshot.evaluated_at)>7200 THEN 'stale' "
            "ELSE 'fresh' END=?"
        )
        parameters.extend((now, now, filters["health_status"]))


def _filter_digest(filters: dict[str, str]) -> str:
    safe_filters = {
        key: (hashlib.sha256(value.encode("utf-8")).hexdigest() if key == "search" else value)
        for key, value in filters.items()
    }
    return hashlib.sha256(
        json.dumps(safe_filters, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode(
            "ascii"
        )
    ).hexdigest()


def _encode_cursor(
    config: AppConfig,
    context: PlatformContext,
    filter_digest: str,
    *,
    updated_at: str,
    workspace_id: str,
) -> str:
    payload = json.dumps(
        {
            "filter_digest": filter_digest,
            "operator_revision": context.operator_revision,
            "platform_session_id": context.platform_session_id,
            "role_digest": _role_digest(context),
            "updated_at": updated_at,
            "v": 1,
            "workspace_id": workspace_id,
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    signature = hmac.new(
        config.platform_session_secret.encode("utf-8"),
        _CURSOR_DOMAIN + b"\0" + payload,
        hashlib.sha256,
    ).digest()
    return _base64_encode(payload) + "." + _base64_encode(signature)


def _decode_cursor(
    config: AppConfig,
    context: PlatformContext,
    cursor: str,
    filter_digest: str,
) -> tuple[str, str]:
    try:
        if len(cursor) > 2048 or cursor.count(".") != 1:
            raise ValueError
        encoded_payload, encoded_signature = cursor.split(".")
        payload = _base64_decode(encoded_payload)
        signature = _base64_decode(encoded_signature)
        expected = hmac.new(
            config.platform_session_secret.encode("utf-8"),
            _CURSOR_DOMAIN + b"\0" + payload,
            hashlib.sha256,
        ).digest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError
        parsed = json.loads(payload.decode("ascii"))
        canonical = json.dumps(
            parsed, ensure_ascii=True, sort_keys=True, separators=(",", ":")
        ).encode("ascii")
        if (
            type(parsed) is not dict
            or canonical != payload
            or set(parsed)
            != {
                "filter_digest",
                "operator_revision",
                "platform_session_id",
                "role_digest",
                "updated_at",
                "v",
                "workspace_id",
            }
            or parsed["v"] != 1
            or parsed["filter_digest"] != filter_digest
            or parsed["operator_revision"] != context.operator_revision
            or parsed["platform_session_id"] != context.platform_session_id
            or parsed["role_digest"] != _role_digest(context)
            or _WORKSPACE_ID_RE.fullmatch(str(parsed["workspace_id"])) is None
        ):
            raise ValueError
        updated_at = str(parsed["updated_at"])
        _parse_time(updated_at)
        return updated_at, str(parsed["workspace_id"])
    except (binascii.Error, UnicodeError, ValueError, TypeError, json.JSONDecodeError):
        raise ApiError("platform.request_invalid") from None


def _request_hash(
    action_type: str, workspace_id: str, expected_revision: int, payload: dict[str, object]
) -> str:
    return hashlib.sha256(
        json.dumps(
            {
                "action_type": action_type,
                "expected_revision": expected_revision,
                "payload": payload,
                "workspace_id": workspace_id,
            },
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
    ).hexdigest()


def _replay[ResponseT: PlatformResponseModel](
    connection: sqlite3.Connection,
    config: AppConfig,
    context: PlatformContext,
    idempotency_key: str,
    request_hash: str,
    response_type: type[ResponseT],
) -> ResponseT | None:
    key_hmac = _idempotency_hmac(config, idempotency_key)
    row = connection.execute(
        "SELECT request_sha256,result_json FROM platform_action_intents "
        "WHERE operator_id=? AND idempotency_key_hmac=?",
        (context.operator_id, key_hmac),
    ).fetchone()
    if row is None:
        return None
    if not hmac.compare_digest(row["request_sha256"], request_hash):
        raise ApiError("platform.idempotency_conflict")
    return response_type.model_validate_json(row["result_json"])


def _insert_intent(
    connection: sqlite3.Connection,
    config: AppConfig,
    context: PlatformContext,
    *,
    intent_id: str,
    workspace_id: str,
    tenant_id: str,
    action_type: str,
    idempotency_key: str,
    request_hash: str,
    expected_revision: int,
    result_revision: int,
    reason_code: str,
    response: PlatformResponseModel,
    request_id: str,
    trace_id: str,
    now: datetime,
) -> None:
    connection.execute(
        """
        INSERT INTO platform_action_intents(
          platform_action_intent_id,operator_id,workspace_id,tenant_id,action_type,
          idempotency_key_hmac,request_sha256,expected_revision,result_revision,
          reason_code,status,result_json,request_id,trace_id,created_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,'completed',?,?,?,?)
        """,
        (
            intent_id,
            context.operator_id,
            workspace_id,
            tenant_id,
            action_type,
            _idempotency_hmac(config, idempotency_key),
            request_hash,
            expected_revision,
            result_revision,
            reason_code,
            response.model_dump_json(),
            request_id,
            trace_id,
            _db_time(now),
        ),
    )


def _write_event(
    connection: sqlite3.Connection,
    state: sqlite3.Row,
    context: PlatformContext,
    intent_id: str,
    event_type: str,
    reason_code: str,
    request_id: str,
    trace_id: str,
    now: datetime,
) -> None:
    operational_revision = int(
        state["operational_revision"]
        if "operational_revision" in state.keys()
        else state["revision"]
    )
    connection.execute(
        """
        INSERT INTO workspace_operational_events(
          workspace_operational_event_id,workspace_id,tenant_id,actor_operator_id,
          action_intent_id,event_type,reason_code,operational_revision,risk_revision,
          request_id,trace_id,occurred_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            _id("woe_"),
            state["workspace_id"],
            state["tenant_id"],
            context.operator_id,
            intent_id,
            event_type,
            reason_code,
            operational_revision,
            int(state["risk_revision"]),
            request_id,
            trace_id,
            _db_time(now),
        ),
    )


def _audit_workspace_action(
    connection: sqlite3.Connection,
    context: PlatformContext,
    action: str,
    workspace_id: str,
    reason_code: str,
    request_id: str,
    trace_id: str,
    now: datetime,
    after: dict[str, object],
) -> None:
    _audit(
        connection,
        actor_domain="platform",
        actor_id=context.operator_id,
        action=action,
        object_type="workspace",
        object_id=workspace_id,
        reason=reason_code,
        request_id=request_id,
        trace_id=trace_id,
        now=now,
        event_type=action,
        after=after,
    )


def _allowed_risk_tags(context: PlatformContext) -> frozenset[str]:
    permissions = set(context.permissions)
    allowed: set[str] = set()
    if "platform.workspace.risk.manage" in permissions:
        allowed.update(_SECURITY_TAGS)
    if "platform.customer_success.read" in permissions:
        allowed.update(_CUSTOMER_SUCCESS_TAGS)
    return frozenset(allowed)


def _require_directory(context: PlatformContext) -> None:
    if not set(context.permissions) & _READ_PERMISSIONS:
        raise ApiError("platform.permission_denied")


def _require_capability(
    context: PlatformContext, capability: Literal["lifecycle", "risk", "contact"]
) -> None:
    if capability == "lifecycle":
        _require_permission(context, "platform.workspace.lifecycle.manage")
        return
    permissions = set(context.permissions)
    if capability == "risk" and permissions & {
        "platform.workspace.risk.manage",
        "platform.customer_success.read",
    }:
        return
    if capability == "contact" and permissions & {
        "platform.workspace.lifecycle.manage",
        "platform.customer_success.read",
    }:
        return
    raise ApiError("platform.permission_denied")


def _finish_workspace_error(connection: sqlite3.Connection, error: ApiError) -> None:
    if not connection.in_transaction:
        return
    if error.code in {"platform.session_expired", "platform.session_revoked"}:
        connection.commit()
    else:
        connection.rollback()


def _connect(config: AppConfig) -> sqlite3.Connection:
    try:
        return connect_database(config.database_url)
    except DbError:
        raise ApiError("platform.workspace_unavailable") from None


def _idempotency_hmac(config: AppConfig, value: str) -> str:
    return hmac.new(
        config.platform_session_secret.encode("utf-8"),
        _IDEMPOTENCY_DOMAIN + b"\0" + value.encode("ascii"),
        hashlib.sha256,
    ).hexdigest()


def _role_digest(context: PlatformContext) -> str:
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


def _id(prefix: str) -> str:
    return prefix + uuid4().hex
