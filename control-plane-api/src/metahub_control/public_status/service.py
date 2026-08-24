"""Database-authoritative PRD-112 incident, maintenance, and subscription services."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import sqlite3
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from metahub_shared.db.connection import connect_database
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError

from metahub_control.notifications.service import enqueue_notification_in_transaction
from metahub_control.platform_access.service import (
    PlatformContext,
    authenticate_platform_session_in_transaction,
)
from metahub_control.workspace_access.service import (
    WorkspaceSessionContext,
    authenticate_workspace_session_in_transaction,
)

from .bundle import publish_status_bundle
from .models import (
    IncidentApprovalRequest,
    IncidentCreateRequest,
    IncidentInfo,
    IncidentResponse,
    IncidentUpdateInfo,
    IncidentUpdateRequest,
    MaintenanceCreateRequest,
    MaintenanceResponse,
    MaintenanceWindowInfo,
    PostmortemInfo,
    PostmortemPublishRequest,
    StatusComponentInfo,
    StatusSubscriptionInfo,
    StatusSubscriptionListResponse,
    StatusSubscriptionMutationRequest,
    StatusSubscriptionMutationResponse,
    StatusWorkspaceResponse,
)

_PUBLIC_CODE = re.compile(r"^[a-z0-9][a-z0-9-]{1,47}$")
_REGION_CODE = re.compile(r"^[a-z0-9][a-z0-9-]{1,39}$")
_IDEMPOTENCY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$")
_TOKEN = re.compile(r"^[A-Za-z0-9_-]{32,256}$")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def list_platform_status(
    config: AppConfig,
    *,
    session_token: str,
    request_id: str,
    trace_id: str,
) -> StatusWorkspaceResponse:
    connection = _connect(config)
    try:
        connection.execute("BEGIN")
        actor = _platform_actor(connection, config, session_token, "platform.status.read")
        result = _status_response(
            connection,
            request_id=request_id,
            trace_id=trace_id,
            workspace=None,
            platform_actor_id=actor.operator_id,
        )
        _audit_read(connection, actor, request_id, trace_id)
        connection.commit()
        return result
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, ValueError, TypeError, KeyError, json.JSONDecodeError):
        _rollback(connection)
        raise ApiError("status.unavailable") from None
    finally:
        connection.close()


def list_workspace_incidents(
    config: AppConfig,
    *,
    session_token: str,
    request_id: str,
    trace_id: str,
) -> StatusWorkspaceResponse:
    connection = _connect(config)
    try:
        connection.execute("BEGIN")
        actor = authenticate_workspace_session_in_transaction(
            connection, config, session_token=session_token, touch=True
        )
        result = _status_response(
            connection,
            request_id=request_id,
            trace_id=trace_id,
            workspace=(actor.workspace_id, actor.tenant_id),
            platform_actor_id=None,
        )
        connection.commit()
        return result
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, ValueError, TypeError, KeyError, json.JSONDecodeError):
        _rollback(connection)
        raise ApiError("status.unavailable") from None
    finally:
        connection.close()


def create_incident(
    config: AppConfig,
    *,
    session_token: str,
    idempotency_key: str,
    payload: IncidentCreateRequest,
    request_id: str,
    trace_id: str,
) -> IncidentResponse:
    _valid_idempotency(idempotency_key)
    _validate_public_fields(
        payload.title,
        payload.initial_update,
        components=payload.affected_components,
        regions=payload.affected_regions,
    )
    request_sha = _digest(payload.model_dump(mode="json"))
    connection = _connect(config)
    publish = False
    replay = False
    operator_id = ""
    incident_id = ""
    try:
        connection.execute("BEGIN IMMEDIATE")
        actor = _platform_actor(connection, config, session_token, "platform.status.write")
        operator_id = actor.operator_id
        replay_id = _command_replay(
            connection, actor, idempotency_key, request_sha, "incident.create"
        )
        if replay_id is not None:
            incident_id = replay_id
            replay = True
            visibility = connection.execute(
                "SELECT visibility FROM incidents WHERE incident_id=?", (incident_id,)
            ).fetchone()
            if visibility is None:
                raise ApiError("status.incident_not_found")
            publish = visibility[0] == "public" and connection.execute(
                "SELECT status FROM incidents WHERE incident_id=?", (incident_id,)
            ).fetchone()[0] != "pending_security_review"
            connection.commit()
        else:
            now = _database_now(connection)
            if payload.acknowledged_at.astimezone(UTC) > now + timedelta(minutes=1):
                raise ApiError("status.request_invalid")
            tenant_id = _workspace_owner(connection, payload.owner_workspace_id)
            _validate_components(connection, payload.affected_components)
            incident_id = "inc_" + uuid4().hex
            status = (
                "published"
                if payload.classification == "reliability"
                else "pending_security_review"
            )
            stamp = _stamp(now)
            first_update = stamp if status == "published" else None
            connection.execute(
                """
                INSERT INTO incidents(
                  incident_id,public_reference,title,classification,visibility,severity,status,
                  acknowledged_at,first_update_at,created_by_operator_id,
                  security_approved_by_operator_id,security_approved_at,revision,
                  created_at,updated_at,resolved_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,NULL,NULL,1,?,?,NULL)
                """,
                (
                    incident_id,
                    _reference("INC", now),
                    payload.title,
                    payload.classification,
                    payload.visibility,
                    payload.severity,
                    status,
                    _stamp(payload.acknowledged_at),
                    first_update,
                    actor.operator_id,
                    stamp,
                    stamp,
                ),
            )
            connection.execute(
                """INSERT INTO incident_workspace_scopes(
                     incident_id,workspace_id,tenant_id,scope_kind,created_at)
                   VALUES(?,?,?,?,?)""",
                (
                    incident_id,
                    payload.owner_workspace_id,
                    tenant_id,
                    "private_projection" if payload.visibility == "workspace" else "owner",
                    stamp,
                ),
            )
            if payload.initial_update is not None:
                _insert_update(
                    connection,
                    incident_id=incident_id,
                    status="published",
                    message=payload.initial_update,
                    components=payload.affected_components,
                    regions=payload.affected_regions,
                    operator_id=actor.operator_id,
                    now=now,
                )
            _insert_event(
                connection,
                event_type="incident.created",
                source_type="incident",
                source_id=incident_id,
                visibility=payload.visibility,
                workspace_id=payload.owner_workspace_id,
                tenant_id=tenant_id,
                request_id=request_id,
                trace_id=trace_id,
                now=now,
            )
            _insert_command(
                connection,
                actor,
                idempotency_key,
                request_sha,
                "incident.create",
                "incident",
                incident_id,
                now,
            )
            _audit(
                connection,
                actor,
                "incident.created",
                "incident",
                incident_id,
                request_id,
                trace_id,
                {"classification": payload.classification, "visibility": payload.visibility},
                now,
            )
            publish = payload.visibility == "public" and status == "published"
            connection.commit()
        if publish:
            _publish(config, operator_id)
        return _incident_response(
            config,
            incident_id,
            replay=replay,
            request_id=request_id,
            trace_id=trace_id,
            operator_id=operator_id,
        )
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, ValueError, TypeError, KeyError):
        _rollback(connection)
        raise ApiError("status.unavailable") from None
    finally:
        connection.close()


def approve_security_incident(
    config: AppConfig,
    *,
    session_token: str,
    incident_id: str,
    idempotency_key: str,
    payload: IncidentApprovalRequest,
    request_id: str,
    trace_id: str,
) -> IncidentResponse:
    return _mutate_incident(
        config,
        session_token=session_token,
        incident_id=incident_id,
        idempotency_key=idempotency_key,
        action="incident.approve",
        permission="platform.status.security_approve",
        payload=payload,
        request_id=request_id,
        trace_id=trace_id,
    )


def update_incident(
    config: AppConfig,
    *,
    session_token: str,
    incident_id: str,
    idempotency_key: str,
    payload: IncidentUpdateRequest,
    request_id: str,
    trace_id: str,
) -> IncidentResponse:
    return _mutate_incident(
        config,
        session_token=session_token,
        incident_id=incident_id,
        idempotency_key=idempotency_key,
        action="incident.update",
        permission="platform.status.write",
        payload=payload,
        request_id=request_id,
        trace_id=trace_id,
    )


def publish_postmortem(
    config: AppConfig,
    *,
    session_token: str,
    incident_id: str,
    idempotency_key: str,
    payload: PostmortemPublishRequest,
    request_id: str,
    trace_id: str,
) -> IncidentResponse:
    _valid_idempotency(idempotency_key)
    _safe_strings((payload.summary, *payload.timeline, *payload.corrective_actions))
    request_sha = _digest(payload.model_dump(mode="json"))
    connection = _connect(config)
    operator_id = ""
    publish = False
    replay = False
    try:
        connection.execute("BEGIN IMMEDIATE")
        actor = _platform_actor(connection, config, session_token, "platform.status.write")
        operator_id = actor.operator_id
        replay_id = _command_replay(
            connection, actor, idempotency_key, request_sha, "postmortem.publish"
        )
        if replay_id is not None:
            row = connection.execute(
                "SELECT incident_id FROM postmortems WHERE postmortem_id=?", (replay_id,)
            ).fetchone()
            if row is None or row[0] != incident_id:
                raise ApiError("status.idempotency_conflict")
            replay = True
        else:
            incident = _incident_row(connection, incident_id)
            if int(incident["revision"]) != payload.revision:
                raise ApiError("status.revision_conflict")
            if incident["status"] != "resolved":
                raise ApiError("status.incident_conflict")
            owner = connection.execute(
                """SELECT workspace_id,tenant_id FROM incident_workspace_scopes
                   WHERE incident_id=? ORDER BY scope_kind LIMIT 1""",
                (incident_id,),
            ).fetchone()
            if owner is None:
                raise ApiError("status.incident_conflict")
            now = _database_now(connection)
            stamp = _stamp(now)
            postmortem_id = "ptm_" + uuid4().hex
            connection.execute(
                """
                INSERT INTO postmortems(
                  postmortem_id,incident_id,workspace_id,tenant_id,public_summary,
                  timeline_json,cause_category,corrective_actions_json,status,
                  published_by_operator_id,revision,created_at,published_at)
                VALUES(?,?,?,?,?,?,?,?,'published',?,1,?,?)
                """,
                (
                    postmortem_id,
                    incident_id,
                    owner["workspace_id"],
                    owner["tenant_id"],
                    payload.summary,
                    _canonical(list(payload.timeline)),
                    payload.cause_category,
                    _canonical(list(payload.corrective_actions)),
                    actor.operator_id,
                    stamp,
                    stamp,
                ),
            )
            connection.execute(
                """UPDATE incidents SET status='postmortem_published',revision=revision+1,
                     updated_at=? WHERE incident_id=? AND revision=?""",
                (stamp, incident_id, payload.revision),
            )
            _insert_command(
                connection,
                actor,
                idempotency_key,
                request_sha,
                "postmortem.publish",
                "postmortem",
                postmortem_id,
                now,
            )
            _audit(
                connection,
                actor,
                "postmortem.published",
                "postmortem",
                postmortem_id,
                request_id,
                trace_id,
                {"incident_id": incident_id},
                now,
            )
        publish = _incident_row(connection, incident_id)["visibility"] == "public"
        connection.commit()
        if publish:
            _publish(config, operator_id)
        return _incident_response(
            config,
            incident_id,
            replay=replay,
            request_id=request_id,
            trace_id=trace_id,
            operator_id=operator_id,
        )
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, ValueError, TypeError, KeyError):
        _rollback(connection)
        raise ApiError("status.unavailable") from None
    finally:
        connection.close()


def create_maintenance_window(
    config: AppConfig,
    *,
    session_token: str,
    idempotency_key: str,
    payload: MaintenanceCreateRequest,
    request_id: str,
    trace_id: str,
) -> MaintenanceResponse:
    _valid_idempotency(idempotency_key)
    _validate_public_fields(
        payload.title,
        payload.public_message,
        components=payload.affected_components,
        regions=payload.affected_regions,
    )
    request_sha = _digest(payload.model_dump(mode="json"))
    connection = _connect(config)
    operator_id = ""
    maintenance_id = ""
    replay = False
    try:
        connection.execute("BEGIN IMMEDIATE")
        actor = _platform_actor(connection, config, session_token, "platform.status.write")
        operator_id = actor.operator_id
        replay_id = _command_replay(
            connection, actor, idempotency_key, request_sha, "maintenance.create"
        )
        if replay_id is not None:
            maintenance_id = replay_id
            replay = True
        else:
            tenant_id = _workspace_owner(connection, payload.owner_workspace_id)
            _validate_components(connection, payload.affected_components)
            now = _database_now(connection)
            stamp = _stamp(now)
            maintenance_id = "mtn_" + uuid4().hex
            connection.execute(
                """
                INSERT INTO maintenance_windows(
                  maintenance_window_id,workspace_id,tenant_id,public_reference,title,
                  public_message,visibility,status,affected_components_json,
                  affected_regions_json,starts_at,ends_at,created_by_operator_id,
                  revision,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,'planned',?,?,?,?,?,1,?,?)
                """,
                (
                    maintenance_id,
                    payload.owner_workspace_id,
                    tenant_id,
                    _reference("MW", now),
                    payload.title,
                    payload.public_message,
                    payload.visibility,
                    _canonical(list(payload.affected_components)),
                    _canonical(list(payload.affected_regions)),
                    _stamp(payload.starts_at),
                    _stamp(payload.ends_at),
                    actor.operator_id,
                    stamp,
                    stamp,
                ),
            )
            _insert_event(
                connection,
                event_type="maintenance.scheduled",
                source_type="maintenance_window",
                source_id=maintenance_id,
                visibility=payload.visibility,
                workspace_id=payload.owner_workspace_id,
                tenant_id=tenant_id,
                request_id=request_id,
                trace_id=trace_id,
                now=now,
            )
            _insert_command(
                connection,
                actor,
                idempotency_key,
                request_sha,
                "maintenance.create",
                "maintenance_window",
                maintenance_id,
                now,
            )
            _audit(
                connection,
                actor,
                "maintenance.scheduled",
                "maintenance_window",
                maintenance_id,
                request_id,
                trace_id,
                {"visibility": payload.visibility},
                now,
            )
        visibility = connection.execute(
            "SELECT visibility FROM maintenance_windows WHERE maintenance_window_id=?",
            (maintenance_id,),
        ).fetchone()
        if visibility is None:
            raise ApiError("status.maintenance_not_found")
        publish = visibility[0] == "public"
        connection.commit()
        if publish:
            _publish(config, operator_id)
        return _maintenance_response(
            config,
            maintenance_id,
            replay=replay,
            request_id=request_id,
            trace_id=trace_id,
        )
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, ValueError, TypeError, KeyError):
        _rollback(connection)
        raise ApiError("status.unavailable") from None
    finally:
        connection.close()


def list_status_subscriptions(
    config: AppConfig,
    *,
    session_token: str,
    request_id: str,
    trace_id: str,
) -> StatusSubscriptionListResponse:
    connection = _connect(config)
    try:
        connection.execute("BEGIN")
        actor = authenticate_workspace_session_in_transaction(
            connection, config, session_token=session_token, touch=True
        )
        rows = connection.execute(
            """SELECT * FROM status_subscriptions
               WHERE workspace_id=? AND tenant_id=? AND account_id=?
               ORDER BY created_at DESC,status_subscription_id DESC""",
            (actor.workspace_id, actor.tenant_id, actor.account_id),
        ).fetchall()
        intent = _create_intent(config, actor)
        connection.commit()
        return StatusSubscriptionListResponse(
            subscriptions=tuple(_subscription_info(row) for row in rows),
            create_intent=intent,
            request_id=request_id,
            trace_id=trace_id,
        )
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, ValueError, TypeError, KeyError):
        _rollback(connection)
        raise ApiError("status.unavailable") from None
    finally:
        connection.close()


def mutate_status_subscription(
    config: AppConfig,
    *,
    payload: StatusSubscriptionMutationRequest,
    request_id: str,
    trace_id: str,
) -> StatusSubscriptionMutationResponse:
    if payload.operation == "create":
        _create_subscription(config, payload)
    else:
        _consume_subscription_token(config, payload.operation, payload.token or "")
    return StatusSubscriptionMutationResponse(
        accepted=True,
        request_id=request_id,
        trace_id=trace_id,
    )


def _mutate_incident(
    config: AppConfig,
    *,
    session_token: str,
    incident_id: str,
    idempotency_key: str,
    action: str,
    permission: str,
    payload: IncidentApprovalRequest | IncidentUpdateRequest,
    request_id: str,
    trace_id: str,
) -> IncidentResponse:
    _valid_idempotency(idempotency_key)
    _validate_public_fields(
        payload.public_message,
        components=payload.affected_components,
        regions=payload.affected_regions,
    )
    request_sha = _digest(payload.model_dump(mode="json"))
    connection = _connect(config)
    operator_id = ""
    replay = False
    try:
        connection.execute("BEGIN IMMEDIATE")
        actor = _platform_actor(connection, config, session_token, permission)
        operator_id = actor.operator_id
        replay_id = _command_replay(connection, actor, idempotency_key, request_sha, action)
        if replay_id is not None:
            update = connection.execute(
                "SELECT incident_id FROM incident_updates WHERE incident_update_id=?",
                (replay_id,),
            ).fetchone()
            if update is None or update[0] != incident_id:
                raise ApiError("status.idempotency_conflict")
            replay = True
        else:
            incident = _incident_row(connection, incident_id)
            if int(incident["revision"]) != payload.revision:
                raise ApiError("status.revision_conflict")
            _validate_components(connection, payload.affected_components)
            now = _database_now(connection)
            stamp = _stamp(now)
            if action == "incident.approve":
                if (
                    incident["classification"] != "security"
                    or incident["status"] != "pending_security_review"
                ):
                    raise ApiError("status.incident_conflict")
                if incident["created_by_operator_id"] == actor.operator_id:
                    raise ApiError("status.approver_conflict")
                next_status = "published"
                connection.execute(
                    """UPDATE incidents SET status='published',first_update_at=?,
                         security_approved_by_operator_id=?,security_approved_at=?,
                         revision=revision+1,updated_at=?
                       WHERE incident_id=? AND revision=?""",
                    (
                        stamp,
                        actor.operator_id,
                        stamp,
                        stamp,
                        incident_id,
                        payload.revision,
                    ),
                )
            else:
                if incident["status"] not in {"published", "monitoring"}:
                    raise ApiError("status.incident_conflict")
                next_status = payload.status
                resolved_at = stamp if next_status == "resolved" else None
                connection.execute(
                    """UPDATE incidents SET status=?,revision=revision+1,updated_at=?,resolved_at=?
                       WHERE incident_id=? AND revision=?""",
                    (next_status, stamp, resolved_at, incident_id, payload.revision),
                )
            update_id = _insert_update(
                connection,
                incident_id=incident_id,
                status=next_status,
                message=payload.public_message,
                components=payload.affected_components,
                regions=payload.affected_regions,
                operator_id=actor.operator_id,
                now=now,
            )
            owner = connection.execute(
                """SELECT workspace_id,tenant_id FROM incident_workspace_scopes
                   WHERE incident_id=? ORDER BY scope_kind LIMIT 1""",
                (incident_id,),
            ).fetchone()
            if owner is None:
                raise ApiError("status.incident_conflict")
            event_type = "incident.resolved" if next_status == "resolved" else "incident.updated"
            _insert_event(
                connection,
                event_type=event_type,
                source_type="incident_update",
                source_id=update_id,
                visibility=incident["visibility"],
                workspace_id=owner["workspace_id"],
                tenant_id=owner["tenant_id"],
                request_id=request_id,
                trace_id=trace_id,
                now=now,
            )
            _insert_command(
                connection,
                actor,
                idempotency_key,
                request_sha,
                action,
                "incident_update",
                update_id,
                now,
            )
            _audit(
                connection,
                actor,
                "incident.security_approved" if action == "incident.approve" else event_type,
                "incident",
                incident_id,
                request_id,
                trace_id,
                {"status": next_status, "visibility": incident["visibility"]},
                now,
            )
        publish = _incident_row(connection, incident_id)["visibility"] == "public"
        connection.commit()
        if publish:
            _publish(config, operator_id)
        return _incident_response(
            config,
            incident_id,
            replay=replay,
            request_id=request_id,
            trace_id=trace_id,
            operator_id=operator_id,
        )
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, ValueError, TypeError, KeyError):
        _rollback(connection)
        raise ApiError("status.unavailable") from None
    finally:
        connection.close()


def _create_subscription(config: AppConfig, payload: StatusSubscriptionMutationRequest) -> None:
    try:
        intent = _decode_intent(config, payload.create_intent or "")
        contact = _normalize_contact(payload.contact or "")
        contact_sha256 = hashlib.sha256(contact.encode()).hexdigest()
        if not hmac.compare_digest(intent["contact_sha256"], contact_sha256):
            return
        scope = _subscription_scope(payload.scope)
    except (ApiError, KeyError, TypeError, ValueError):
        return
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        owner = connection.execute(
            """SELECT 1 FROM workspace_tenant_bindings binding
               JOIN workspace_memberships membership ON
                 membership.workspace_id=binding.workspace_id
                 AND membership.tenant_id=binding.tenant_id
               WHERE binding.workspace_id=? AND binding.tenant_id=?
                 AND membership.account_id=? AND membership.status='active'""",
            (intent["workspace_id"], intent["tenant_id"], intent["account_id"]),
        ).fetchone()
        if owner is None:
            connection.rollback()
            return
        now = _database_now(connection)
        blind = _contact_blind(config, contact)
        existing = connection.execute(
            """SELECT status_subscription_id,status FROM status_subscriptions
               WHERE workspace_id=? AND contact_blind_index=? AND channel='email'""",
            (intent["workspace_id"], blind),
        ).fetchone()
        if existing is not None:
            connection.commit()
            return
        token = secrets.token_urlsafe(32)
        subscription_id = "sts_" + uuid4().hex
        stamp = _stamp(now)
        connection.execute(
            """
            INSERT INTO status_subscriptions(
              status_subscription_id,workspace_id,tenant_id,account_id,channel,
              contact_ciphertext,contact_blind_index,scope_json,verification_token_hash,
              unsubscribe_token_hash,status,verification_expires_at,verified_at,
              unsubscribed_at,revision,created_at,updated_at)
            VALUES(?,?,?,?, 'email',?,?,?,?,NULL,'pending',?,NULL,NULL,1,?,?)
            """,
            (
                subscription_id,
                intent["workspace_id"],
                intent["tenant_id"],
                intent["account_id"],
                _encrypt_contact(config, contact, subscription_id),
                blind,
                _canonical(scope),
                _token_hash(config, "verify", token),
                _stamp(now + timedelta(hours=1)),
                stamp,
                stamp,
            ),
        )
        enqueue_notification_in_transaction(
            connection,
            config,
            workspace_id=intent["workspace_id"],
            tenant_id=intent["tenant_id"],
            recipient_account_id=intent["account_id"],
            event_type="verification.status_changed",
            source_owner_type="status_subscription",
            source_owner_id=subscription_id,
            parameters={
                "status": f"Verify status updates: /status.html?verify={token}",
                "updated_at": stamp,
            },
            now=now,
        )
        connection.commit()
    except ApiError:
        _rollback(connection)
        raise ApiError("status.unavailable") from None
    except (sqlite3.Error, ValueError, TypeError, KeyError):
        _rollback(connection)
        raise ApiError("status.unavailable") from None
    finally:
        connection.close()


def _consume_subscription_token(config: AppConfig, operation: str, token: str) -> None:
    if _TOKEN.fullmatch(token) is None:
        return
    digest = _token_hash(config, "verify" if operation == "verify" else "unsubscribe", token)
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        if operation == "verify":
            row = connection.execute(
                """SELECT * FROM status_subscriptions
                   WHERE verification_token_hash=? AND status='pending'
                     AND unixepoch(verification_expires_at)>unixepoch('now')""",
                (digest,),
            ).fetchone()
            if row is not None:
                now = _database_now(connection)
                connection.execute(
                    """UPDATE status_subscriptions SET status='active',
                         verification_token_hash=NULL,unsubscribe_token_hash=?,
                         verification_expires_at=NULL,verified_at=?,revision=revision+1,
                         updated_at=? WHERE status_subscription_id=?""",
                    (
                        _token_hash(config, "unsubscribe", token),
                        _stamp(now),
                        _stamp(now),
                        row["status_subscription_id"],
                    ),
                )
        else:
            row = connection.execute(
                """SELECT status_subscription_id FROM status_subscriptions
                   WHERE unsubscribe_token_hash=? AND status='active'""",
                (digest,),
            ).fetchone()
            if row is not None:
                now = _database_now(connection)
                connection.execute(
                    """UPDATE status_subscriptions SET status='unsubscribed',
                         unsubscribe_token_hash=NULL,unsubscribed_at=?,revision=revision+1,
                         updated_at=? WHERE status_subscription_id=?""",
                    (_stamp(now), _stamp(now), row["status_subscription_id"]),
                )
        connection.commit()
    except (sqlite3.Error, ValueError, TypeError, KeyError):
        _rollback(connection)
        raise ApiError("status.unavailable") from None
    finally:
        connection.close()


def _status_response(
    connection: sqlite3.Connection,
    *,
    request_id: str,
    trace_id: str,
    workspace: tuple[str, str] | None,
    platform_actor_id: str | None,
) -> StatusWorkspaceResponse:
    components = tuple(
        StatusComponentInfo(
            code=row["public_code"],
            name=row["public_name"],
            group=row["component_group"],
            status=row["current_state"],
            revision=int(row["revision"]),
        )
        for row in connection.execute(
            "SELECT * FROM status_components ORDER BY display_order"
        ).fetchall()
    )
    parameters: tuple[object, ...] = ()
    predicate = "1=1"
    if workspace is not None:
        predicate = """(incident.visibility='public' OR EXISTS(
          SELECT 1 FROM incident_workspace_scopes scope WHERE scope.incident_id=incident.incident_id
            AND scope.workspace_id=? AND scope.tenant_id=?))"""
        parameters = workspace
    incidents = tuple(
        _incident_info(connection, row, platform_actor_id=platform_actor_id)
        for row in connection.execute(
            f"""SELECT incident.* FROM incidents incident WHERE {predicate}
                ORDER BY incident.updated_at DESC,incident.incident_id DESC LIMIT 500""",
            parameters,
        ).fetchall()
    )
    maintenance_predicate = "1=1"
    if workspace is not None:
        maintenance_predicate = "visibility='public' OR(workspace_id=? AND tenant_id=?)"
    maintenance = tuple(
        _maintenance_info(row)
        for row in connection.execute(
            f"""SELECT * FROM maintenance_windows WHERE {maintenance_predicate}
                ORDER BY starts_at DESC,maintenance_window_id DESC LIMIT 500""",
            parameters,
        ).fetchall()
    )
    head = connection.execute(
        "SELECT generation FROM status_publication_bundles WHERE status='published'"
    ).fetchone()
    return StatusWorkspaceResponse(
        components=components,
        incidents=incidents,
        maintenance_windows=maintenance,
        bundle_generation=None if head is None else int(head[0]),
        request_id=request_id,
        trace_id=trace_id,
    )


def _incident_response(
    config: AppConfig,
    incident_id: str,
    *,
    replay: bool,
    request_id: str,
    trace_id: str,
    operator_id: str,
) -> IncidentResponse:
    connection = _connect(config)
    try:
        row = _incident_row(connection, incident_id)
        return IncidentResponse(
            incident=_incident_info(connection, row, platform_actor_id=operator_id),
            idempotent_replay=replay,
            request_id=request_id,
            trace_id=trace_id,
        )
    except ApiError:
        raise
    except (sqlite3.Error, ValueError, TypeError, KeyError, json.JSONDecodeError):
        raise ApiError("status.unavailable") from None
    finally:
        connection.close()


def _maintenance_response(
    config: AppConfig,
    maintenance_id: str,
    *,
    replay: bool,
    request_id: str,
    trace_id: str,
) -> MaintenanceResponse:
    connection = _connect(config)
    try:
        row = connection.execute(
            "SELECT * FROM maintenance_windows WHERE maintenance_window_id=?",
            (maintenance_id,),
        ).fetchone()
        if row is None:
            raise ApiError("status.maintenance_not_found")
        return MaintenanceResponse(
            maintenance_window=_maintenance_info(row),
            idempotent_replay=replay,
            request_id=request_id,
            trace_id=trace_id,
        )
    finally:
        connection.close()


def _incident_info(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    platform_actor_id: str | None,
) -> IncidentInfo:
    updates = tuple(
        IncidentUpdateInfo(
            incident_update_id=item["incident_update_id"],
            status=item["status"],
            message=item["public_message"],
            affected_components=tuple(_json_list(item["affected_components_json"])),
            affected_regions=tuple(_json_list(item["affected_regions_json"])),
            published_at=_time(item["published_at"]),
        )
        for item in connection.execute(
            """SELECT * FROM incident_updates WHERE incident_id=?
               ORDER BY published_at,incident_update_id""",
            (row["incident_id"],),
        ).fetchall()
    )
    postmortem_row = connection.execute(
        "SELECT * FROM postmortems WHERE incident_id=? AND status='published'",
        (row["incident_id"],),
    ).fetchone()
    postmortem = None
    if postmortem_row is not None:
        postmortem = PostmortemInfo(
            summary=postmortem_row["public_summary"],
            timeline=tuple(_json_list(postmortem_row["timeline_json"])),
            cause_category=postmortem_row["cause_category"],
            corrective_actions=tuple(_json_list(postmortem_row["corrective_actions_json"])),
            published_at=_time(postmortem_row["published_at"]),
        )
    return IncidentInfo(
        incident_id=row["incident_id"],
        reference=row["public_reference"],
        title=row["title"],
        classification=row["classification"],
        visibility=row["visibility"],
        severity=row["severity"],
        status=row["status"],
        updates=updates,
        postmortem=postmortem,
        security_approval_available=(
            platform_actor_id is not None
            and row["status"] == "pending_security_review"
            and row["created_by_operator_id"] != platform_actor_id
        ),
        revision=int(row["revision"]),
        acknowledged_at=_time(row["acknowledged_at"]),
        updated_at=_time(row["updated_at"]),
    )


def _maintenance_info(row: sqlite3.Row) -> MaintenanceWindowInfo:
    return MaintenanceWindowInfo(
        maintenance_window_id=row["maintenance_window_id"],
        reference=row["public_reference"],
        title=row["title"],
        message=row["public_message"],
        visibility=row["visibility"],
        status=row["status"],
        affected_components=tuple(_json_list(row["affected_components_json"])),
        affected_regions=tuple(_json_list(row["affected_regions_json"])),
        starts_at=_time(row["starts_at"]),
        ends_at=_time(row["ends_at"]),
        revision=int(row["revision"]),
    )


def _subscription_info(row: sqlite3.Row) -> StatusSubscriptionInfo:
    scope = json.loads(row["scope_json"])
    if type(scope) is not dict:
        raise ValueError
    return StatusSubscriptionInfo(
        status_subscription_id=row["status_subscription_id"],
        channel=row["channel"],
        scope=scope,
        status=row["status"],
        revision=int(row["revision"]),
        created_at=_time(row["created_at"]),
    )


def _platform_actor(
    connection: sqlite3.Connection,
    config: AppConfig,
    session_token: str,
    permission: str,
) -> PlatformContext:
    actor = authenticate_platform_session_in_transaction(
        connection, config, session_token=session_token, touch=True
    )
    if permission not in actor.permissions:
        raise ApiError("platform.permission_denied")
    return actor


def _workspace_owner(connection: sqlite3.Connection, workspace_id: str) -> str:
    row = connection.execute(
        "SELECT tenant_id FROM workspace_tenant_bindings WHERE workspace_id=?", (workspace_id,)
    ).fetchone()
    if row is None:
        raise ApiError("status.owner_not_found")
    return str(row[0])


def _incident_row(connection: sqlite3.Connection, incident_id: str) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM incidents WHERE incident_id=?", (incident_id,)
    ).fetchone()
    if row is None:
        raise ApiError("status.incident_not_found")
    return row


def _insert_update(
    connection: sqlite3.Connection,
    *,
    incident_id: str,
    status: str,
    message: str,
    components: Sequence[str],
    regions: Sequence[str],
    operator_id: str,
    now: datetime,
) -> str:
    update_id = "iup_" + uuid4().hex
    body = {
        "incident_id": incident_id,
        "status": status,
        "message": message,
        "affected_components": list(components),
        "affected_regions": list(regions),
        "published_at": _stamp(now),
    }
    connection.execute(
        """
        INSERT INTO incident_updates(
          incident_update_id,incident_id,status,public_message,affected_components_json,
          affected_regions_json,operator_id,published_at,payload_sha256)
        VALUES(?,?,?,?,?,?,?,?,?)
        """,
        (
            update_id,
            incident_id,
            status,
            message,
            _canonical(list(components)),
            _canonical(list(regions)),
            operator_id,
            _stamp(now),
            _digest(body),
        ),
    )
    return update_id


def _insert_event(
    connection: sqlite3.Connection,
    *,
    event_type: str,
    source_type: str,
    source_id: str,
    visibility: str,
    workspace_id: str,
    tenant_id: str,
    request_id: str,
    trace_id: str,
    now: datetime,
) -> None:
    public = visibility == "public"
    safe = {"event_type": event_type, "source_id": source_id, "visibility": visibility}
    connection.execute(
        """
        INSERT INTO status_event_outbox(
          status_event_id,event_type,source_object_type,source_object_id,visibility,
          workspace_id,tenant_id,payload_sha256,request_id,trace_id,occurred_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            "sev_" + uuid4().hex,
            event_type,
            source_type,
            source_id,
            visibility,
            None if public else workspace_id,
            None if public else tenant_id,
            _digest(safe),
            request_id,
            trace_id,
            _stamp(now),
        ),
    )


def _command_replay(
    connection: sqlite3.Connection,
    actor: PlatformContext,
    key: str,
    request_sha: str,
    action: str,
) -> str | None:
    row = connection.execute(
        """SELECT * FROM status_commands
           WHERE operator_id=? AND idempotency_sha256=?""",
        (actor.operator_id, _idempotency_hash(key)),
    ).fetchone()
    if row is None:
        return None
    if row["request_sha256"] != request_sha or row["action_code"] != action:
        raise ApiError("status.idempotency_conflict")
    return str(row["result_object_id"])


def _insert_command(
    connection: sqlite3.Connection,
    actor: PlatformContext,
    key: str,
    request_sha: str,
    action: str,
    result_type: str,
    result_id: str,
    now: datetime,
) -> None:
    connection.execute(
        """INSERT INTO status_commands(
             status_command_id,operator_id,action_code,idempotency_sha256,request_sha256,
             result_object_type,result_object_id,created_at)
           VALUES(?,?,?,?,?,?,?,?)""",
        (
            "scm_" + uuid4().hex,
            actor.operator_id,
            action,
            _idempotency_hash(key),
            request_sha,
            result_type,
            result_id,
            _stamp(now),
        ),
    )


def _publish(config: AppConfig, operator_id: str) -> None:
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        publish_status_bundle(
            connection, config, operator_id=operator_id, now=_database_now(connection)
        )
        connection.commit()
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, ValueError, TypeError, KeyError):
        _rollback(connection)
        raise ApiError("status.publication_unavailable") from None
    finally:
        connection.close()


def _create_intent(config: AppConfig, actor: WorkspaceSessionContext) -> str:
    body = _canonical(
        {
            "account_id": actor.account_id,
            "contact_sha256": hashlib.sha256(
                _normalize_contact(actor.account_email).encode("utf-8")
            ).hexdigest(),
            "expires_at": _stamp(datetime.now(UTC) + timedelta(minutes=15)),
            "nonce": secrets.token_hex(16),
            "tenant_id": actor.tenant_id,
            "version": 1,
            "workspace_id": actor.workspace_id,
        }
    )
    signature = hmac.new(
        config.platform_session_secret.encode(),
        b"prd112/status-subscription-intent/v1\0" + body.encode("ascii"),
        hashlib.sha256,
    ).hexdigest()
    return _b64(f"{body}.{signature}".encode("ascii"))


def _decode_intent(config: AppConfig, value: str) -> dict[str, str]:
    try:
        raw = _unb64(value).decode("ascii")
        body, signature = raw.rsplit(".", 1)
        expected = hmac.new(
            config.platform_session_secret.encode(),
            b"prd112/status-subscription-intent/v1\0" + body.encode("ascii"),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError
        parsed = json.loads(body)
        if type(parsed) is not dict or set(parsed) != {
            "account_id",
            "contact_sha256",
            "expires_at",
            "nonce",
            "tenant_id",
            "version",
            "workspace_id",
        }:
            raise ValueError
        if parsed["version"] != 1 or _time(parsed["expires_at"]) <= datetime.now(UTC):
            raise ValueError
        return {key: str(item) for key, item in parsed.items()}
    except Exception:
        raise ApiError("status.intent_invalid") from None


def _subscription_scope(value: object) -> dict[str, object]:
    scope = value if value is not None else {"components": [], "regions": [], "severities": []}
    if type(scope) is not dict or set(scope) != {"components", "regions", "severities"}:
        raise ValueError
    components = scope["components"]
    regions = scope["regions"]
    severities = scope["severities"]
    if (
        type(components) is not list
        or type(regions) is not list
        or type(severities) is not list
        or len(components) > 50
        or len(regions) > 50
        or len(severities) > 3
        or any(type(item) is not str or _PUBLIC_CODE.fullmatch(item) is None for item in components)
        or any(type(item) is not str or _REGION_CODE.fullmatch(item) is None for item in regions)
        or any(item not in {"minor", "major", "critical"} for item in severities)
    ):
        raise ValueError
    return {
        "components": sorted(set(components)),
        "regions": sorted(set(regions)),
        "severities": sorted(set(severities)),
    }


def _encrypt_contact(config: AppConfig, contact: str, subscription_id: str) -> str:
    nonce = secrets.token_bytes(12)
    plaintext = _canonical({"contact": contact}).encode("ascii")
    ciphertext = AESGCM(_contact_key(config)).encrypt(
        nonce, plaintext, f"prd112/status-contact/v1\0{subscription_id}".encode("ascii")
    )
    return "s1." + _b64(nonce + ciphertext)


def _contact_key(config: AppConfig) -> bytes:
    secret = config.identity_pii_encryption_secret
    if not secret:
        raise ApiError("status.unavailable")
    return hmac.new(
        secret.encode(), b"prd112/status-contact-encryption/v1", hashlib.sha256
    ).digest()


def _contact_blind(config: AppConfig, contact: str) -> str:
    return hmac.new(
        _contact_key(config), b"prd112/status-contact-index/v1\0" + contact.encode(), hashlib.sha256
    ).hexdigest()


def _token_hash(config: AppConfig, purpose: str, token: str) -> str:
    return hmac.new(
        config.platform_session_secret.encode(),
        f"prd112/status-subscription-{purpose}/v1\0{token}".encode(),
        hashlib.sha256,
    ).hexdigest()


def _normalize_contact(value: str) -> str:
    normalized = value.strip().casefold()
    if (
        not 3 <= len(normalized) <= 320
        or normalized.count("@") != 1
        or " " in normalized
        or _CONTROL.search(normalized)
    ):
        raise ValueError
    return normalized


def _validate_public_fields(
    *values: str | None,
    components: Sequence[str],
    regions: Sequence[str],
) -> None:
    _safe_strings(tuple(value for value in values if value is not None))
    if (
        len(set(components)) != len(components)
        or len(set(regions)) != len(regions)
        or any(_PUBLIC_CODE.fullmatch(value) is None for value in components)
        or any(_REGION_CODE.fullmatch(value) is None for value in regions)
    ):
        raise ApiError("status.request_invalid")


def _safe_strings(values: Sequence[str]) -> None:
    if any("<" in value or ">" in value or _CONTROL.search(value) for value in values):
        raise ApiError("status.request_invalid")


def _validate_components(connection: sqlite3.Connection, components: Sequence[str]) -> None:
    if not components:
        return
    placeholders = ",".join("?" for _ in components)
    found = int(
        connection.execute(
            f"SELECT COUNT(*) FROM status_components WHERE public_code IN({placeholders})",
            components,
        ).fetchone()[0]
    )
    if found != len(components):
        raise ApiError("status.component_not_found")


def _audit(
    connection: sqlite3.Connection,
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
        """INSERT INTO platform_audit_logs(
             audit_id,actor_domain,actor_id,action_code,object_type,object_id,reason_code,
             request_id,trace_id,before_sha256,after_sha256,occurred_at)
           VALUES(?,'platform',?,?,?,?,?,?,?,NULL,?,?)""",
        (
            "pau_" + uuid4().hex,
            actor.operator_id,
            action,
            object_type,
            object_id,
            "public_communication",
            request_id,
            trace_id,
            _digest(after),
            _stamp(now),
        ),
    )


def _audit_read(
    connection: sqlite3.Connection,
    actor: PlatformContext,
    request_id: str,
    trace_id: str,
) -> None:
    _audit(
        connection,
        actor,
        "status.read",
        "status_projection",
        "status-public-projection",
        request_id,
        trace_id,
        {"projection": "platform"},
        _database_now(connection),
    )


def _valid_idempotency(value: str) -> None:
    if _IDEMPOTENCY.fullmatch(value) is None:
        raise ApiError("status.request_invalid")


def _idempotency_hash(value: str) -> str:
    return hashlib.sha256(f"prd112/status-idempotency/v1\0{value}".encode()).hexdigest()


def _reference(prefix: str, now: datetime) -> str:
    return f"{prefix}-{now:%Y%m%d}-{secrets.token_hex(3).upper()}"


def _json_list(value: object) -> list[str]:
    parsed = json.loads(str(value))
    if type(parsed) is not list or any(type(item) is not str for item in parsed):
        raise ValueError
    return parsed


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode("ascii")).hexdigest()


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _unb64(value: str) -> bytes:
    if not value or "=" in value:
        raise ValueError
    return base64.b64decode(
        value + "=" * ((4 - len(value) % 4) % 4), altchars=b"-_", validate=True
    )


def _database_now(connection: sqlite3.Connection) -> datetime:
    return _time(connection.execute("SELECT strftime('%Y-%m-%dT%H:%M:%fZ','now')").fetchone()[0])


def _time(value: object) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _stamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _connect(config: AppConfig) -> sqlite3.Connection:
    try:
        return connect_database(config.database_url)
    except Exception:
        raise ApiError("status.unavailable") from None


def _rollback(connection: sqlite3.Connection) -> None:
    try:
        if connection.in_transaction:
            connection.rollback()
    except Exception:
        pass


__all__ = [
    "approve_security_incident",
    "create_incident",
    "create_maintenance_window",
    "list_platform_status",
    "list_status_subscriptions",
    "list_workspace_incidents",
    "mutate_status_subscription",
    "publish_postmortem",
    "update_incident",
]
