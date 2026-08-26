"""Transactional signed desired state, Agent ACKs, and LKG rollback for PRD-121."""

from __future__ import annotations

import hashlib
import hmac
import re
import sqlite3
from datetime import UTC, datetime, timedelta

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from metahub_shared.db.errors import DbError
from metahub_shared.private_gateway import (
    canonical_edge_json_bytes,
    public_key_b64,
    sign_desired_state,
    sign_trust_manifest,
    strict_json_object,
)
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError

from metahub_control.config_distribution.crypto import open_config_value
from metahub_control.workspace_access.service import WorkspaceSessionContext

from .models import (
    EdgeAckCreateRequest,
    EdgeAckResponse,
    EdgeDesiredStateApprovalRequest,
    EdgeDesiredStatePublishRequest,
    EdgeDesiredStatePullResponse,
    EdgeDesiredStateResponse,
    EdgeDesiredStateRollbackRequest,
    EdgeDesiredStateSummary,
)
from .service import (
    _connect,
    _datetime,
    _db_time,
    _id,
    _identifier,
    _optional_datetime,
    _owned_deployment,
    _rollback,
    _secret_hmac,
    _utcnow,
    _workspace_context,
)

_DEPLOYMENT_RE = re.compile(r"^edp_[0-9a-f]{32}$")
_IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$")
_SESSION_RE = re.compile(r"^egs_[A-Za-z0-9_-]{43}$")
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_TRUST_REFRESH_WINDOW = timedelta(days=7)
_TRUST_TTL = timedelta(days=365)
_SECURITY_TTL = timedelta(hours=24)
_CONFIGURATION_TTL = timedelta(days=7)

canonical_json_bytes = canonical_edge_json_bytes


def get_workspace_desired_state(
    config: AppConfig,
    deployment_id: str,
    *,
    session_token: str,
    request_id: str,
    trace_id: str,
) -> EdgeDesiredStateResponse:
    _deployment_id(deployment_id)
    connection = _connect(config)
    try:
        connection.execute("BEGIN")
        context = _workspace_context(
            connection,
            config,
            session_token=session_token,
            csrf_token=None,
            permission="edge.deployment.read",
        )
        _owned_deployment(connection, context, deployment_id)
        state = _latest_state(connection, context, deployment_id)
        connection.commit()
        return EdgeDesiredStateResponse(
            state=state,
            request_id=_identifier(request_id),
            trace_id=_identifier(trace_id),
        )
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, DbError, KeyError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("edge.unavailable") from None
    finally:
        connection.close()


def write_workspace_desired_state(
    config: AppConfig,
    deployment_id: str,
    payload: EdgeDesiredStatePublishRequest | EdgeDesiredStateApprovalRequest,
    *,
    session_token: str,
    csrf_token: str,
    expected_revision: int,
    idempotency_key: str,
    request_id: str,
    trace_id: str,
) -> EdgeDesiredStateResponse:
    _deployment_id(deployment_id)
    _idempotency_key(idempotency_key)
    action = (
        "edge.desired_state.publish"
        if isinstance(payload, EdgeDesiredStatePublishRequest)
        else f"edge.desired_state.{payload.action}"
    )
    request_sha256 = _request_digest(payload.model_dump(mode="json"))
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        context = _workspace_context(
            connection,
            config,
            session_token=session_token,
            csrf_token=csrf_token,
            permission="edge.deployment.manage",
        )
        deployment = _owned_deployment(connection, context, deployment_id)
        if str(deployment["status"]) != "active":
            raise ApiError("edge.state_conflict")
        idempotency_sha256 = _workspace_idempotency(config, context, action, idempotency_key)
        replay = _command(connection, context, action, idempotency_sha256)
        if replay is not None:
            if not hmac.compare_digest(str(replay["request_sha256"]), request_sha256):
                raise ApiError("edge.idempotency_conflict")
            state = _state_by_id(
                connection,
                context,
                deployment_id,
                str(replay["result_desired_state_id"]),
            )
            connection.commit()
            return EdgeDesiredStateResponse(
                state=state,
                idempotent_replay=True,
                request_id=_identifier(request_id),
                trace_id=_identifier(trace_id),
            )

        if isinstance(payload, EdgeDesiredStatePublishRequest):
            state_id = _request_publish(
                connection,
                config,
                context,
                deployment,
                payload,
                expected_revision=expected_revision,
                request_id=request_id,
                trace_id=trace_id,
            )
        else:
            state_id = _decide_pending_state(
                connection,
                config,
                context,
                deployment,
                payload,
                expected_revision=expected_revision,
                request_id=request_id,
                trace_id=trace_id,
            )
        now = _db_time(_utcnow())
        _insert_command(
            connection,
            context,
            deployment_id=deployment_id,
            action=action,
            idempotency_sha256=idempotency_sha256,
            request_sha256=request_sha256,
            result_desired_state_id=state_id,
            now=now,
        )
        state = _state_by_id(connection, context, deployment_id, state_id)
        connection.commit()
        return EdgeDesiredStateResponse(
            state=state,
            request_id=_identifier(request_id),
            trace_id=_identifier(trace_id),
        )
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, DbError, KeyError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("edge.unavailable") from None
    finally:
        connection.close()


def rollback_workspace_desired_state(
    config: AppConfig,
    deployment_id: str,
    payload: EdgeDesiredStateRollbackRequest,
    *,
    session_token: str,
    csrf_token: str,
    expected_revision: int,
    idempotency_key: str,
    request_id: str,
    trace_id: str,
) -> EdgeDesiredStateResponse:
    _deployment_id(deployment_id)
    _idempotency_key(idempotency_key)
    action = "edge.desired_state.rollback"
    request_sha256 = _request_digest(payload.model_dump(mode="json"))
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        context = _workspace_context(
            connection,
            config,
            session_token=session_token,
            csrf_token=csrf_token,
            permission="edge.deployment.manage",
        )
        deployment = _owned_deployment(connection, context, deployment_id)
        if str(deployment["status"]) != "active":
            raise ApiError("edge.state_conflict")
        idempotency_sha256 = _workspace_idempotency(config, context, action, idempotency_key)
        replay = _command(connection, context, action, idempotency_sha256)
        if replay is not None:
            if not hmac.compare_digest(str(replay["request_sha256"]), request_sha256):
                raise ApiError("edge.idempotency_conflict")
            state = _state_by_id(
                connection,
                context,
                deployment_id,
                str(replay["result_desired_state_id"]),
            )
            connection.commit()
            return EdgeDesiredStateResponse(
                state=state,
                idempotent_replay=True,
                request_id=_identifier(request_id),
                trace_id=_identifier(trace_id),
            )
        _ensure_no_pending(connection, deployment_id)
        head = connection.execute(
            "SELECT * FROM edge_desired_state_heads WHERE edge_deployment_id=?",
            (deployment_id,),
        ).fetchone()
        if (
            head is None
            or int(head["revision"]) != expected_revision
            or int(head["lkg_generation"]) != payload.lkg_generation
            or payload.lkg_generation <= 0
        ):
            raise ApiError("edge.revision_conflict")
        source = connection.execute(
            """
            SELECT desired.*,artifact.content_rfc8785,artifact.content_sha256
            FROM edge_signed_snapshots snapshot
            JOIN edge_desired_states desired
              ON desired.edge_desired_state_id=snapshot.edge_desired_state_id
            JOIN edge_snapshot_artifacts artifact
              ON artifact.edge_snapshot_artifact_id=snapshot.edge_snapshot_artifact_id
            WHERE snapshot.workspace_id=? AND snapshot.tenant_id=?
              AND snapshot.edge_deployment_id=? AND snapshot.generation=?
              AND snapshot.envelope_sha256=?
            """,
            (
                context.workspace_id,
                context.tenant_id,
                deployment_id,
                payload.lkg_generation,
                head["lkg_envelope_sha256"],
            ),
        ).fetchone()
        if source is None:
            raise ApiError("edge.state_conflict")
        generation = _next_generation(connection, deployment_id)
        now = _db_time(_utcnow())
        state_id = _id("edst_")
        connection.execute(
            """
            INSERT INTO edge_desired_states(
              edge_desired_state_id,workspace_id,tenant_id,edge_deployment_id,generation,
              base_generation,rollback_of_generation,source_config_snapshot_id,
              source_generation,source_manifest_sha256,source_revocation_watermark,
              change_class,status,requested_by_account_id,approved_by_account_id,revision,
              request_sha256,requested_at,decided_at,published_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,'standard','published',?,NULL,1,?,?,NULL,?,?)
            """,
            (
                state_id,
                context.workspace_id,
                context.tenant_id,
                deployment_id,
                generation,
                int(head["generation"]),
                payload.lkg_generation,
                source["source_config_snapshot_id"],
                source["source_generation"],
                source["source_manifest_sha256"],
                source["source_revocation_watermark"],
                context.account_id,
                request_sha256,
                now,
                now,
                now,
            ),
        )
        content = strict_json_object(str(source["content_rfc8785"]).encode("utf-8"), canonical=True)
        if hashlib.sha256(canonical_json_bytes(content)).hexdigest() != str(
            source["content_sha256"]
        ):
            raise ApiError("edge.unavailable")
        _materialize(
            connection,
            config,
            context,
            deployment,
            state_id,
            content_override=content,
            event_reason="published",
            request_id=request_id,
            trace_id=trace_id,
        )
        _insert_command(
            connection,
            context,
            deployment_id=deployment_id,
            action=action,
            idempotency_sha256=idempotency_sha256,
            request_sha256=request_sha256,
            result_desired_state_id=state_id,
            now=_db_time(_utcnow()),
        )
        state = _state_by_id(connection, context, deployment_id, state_id)
        connection.commit()
        return EdgeDesiredStateResponse(
            state=state,
            request_id=_identifier(request_id),
            trace_id=_identifier(trace_id),
        )
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, DbError, KeyError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("edge.unavailable") from None
    finally:
        connection.close()


def pull_agent_desired_state(
    config: AppConfig,
    deployment_id: str,
    *,
    verified_fingerprint: str,
    edge_session_token: str,
    request_id: str,
    trace_id: str,
) -> EdgeDesiredStatePullResponse:
    _deployment_id(deployment_id)
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        agent = _authenticated_agent(
            connection,
            config,
            deployment_id=deployment_id,
            verified_fingerprint=verified_fingerprint,
            edge_session_token=edge_session_token,
        )
        row = connection.execute(
            """
            SELECT head.generation,head.desired_envelope_sha256,
              snapshot.envelope_rfc8785,manifest.manifest_rfc8785
            FROM edge_desired_state_heads head
            JOIN edge_signed_snapshots snapshot
              ON snapshot.edge_signed_snapshot_id=head.edge_signed_snapshot_id
            JOIN edge_trust_manifests manifest
              ON manifest.edge_trust_manifest_id=snapshot.edge_trust_manifest_id
            WHERE head.workspace_id=? AND head.tenant_id=? AND head.edge_deployment_id=?
            """,
            (agent["workspace_id"], agent["tenant_id"], deployment_id),
        ).fetchone()
        if row is None:
            raise ApiError("edge.state_conflict")
        snapshot = strict_json_object(str(row["envelope_rfc8785"]).encode("utf-8"), canonical=True)
        manifest = strict_json_object(str(row["manifest_rfc8785"]).encode("utf-8"), canonical=True)
        envelope_sha256 = hashlib.sha256(canonical_json_bytes(snapshot)).hexdigest()
        if not hmac.compare_digest(envelope_sha256, str(row["desired_envelope_sha256"])):
            raise ApiError("edge.unavailable")
        now = _db_time(_utcnow())
        connection.execute(
            "UPDATE edge_deployments SET last_connected_at=?,health_code='healthy',updated_at=? "
            "WHERE edge_deployment_id=? AND status='active'",
            (now, now, deployment_id),
        )
        connection.commit()
        return EdgeDesiredStatePullResponse(
            deployment_id=deployment_id,
            generation=int(row["generation"]),
            envelope_sha256=envelope_sha256,
            snapshot=snapshot,
            trust_manifest=manifest,
            request_id=_identifier(request_id),
            trace_id=_identifier(trace_id),
        )
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, DbError, KeyError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("edge.unavailable") from None
    finally:
        connection.close()


def acknowledge_agent_desired_state(
    config: AppConfig,
    deployment_id: str,
    payload: EdgeAckCreateRequest,
    *,
    verified_fingerprint: str,
    edge_session_token: str,
    request_id: str,
    trace_id: str,
) -> EdgeAckResponse:
    _deployment_id(deployment_id)
    _validate_ack(payload)
    request_sha256 = _request_digest(payload.model_dump(mode="json"))
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        agent = _authenticated_agent(
            connection,
            config,
            deployment_id=deployment_id,
            verified_fingerprint=verified_fingerprint,
            edge_session_token=edge_session_token,
        )
        nonce_hmac = _secret_hmac(
            config, f"edge-desired-ack-nonce-v1\0{deployment_id}", payload.attempt_nonce
        )
        existing = connection.execute(
            """
            SELECT ack.*,head.generation desired_generation,head.applied_generation,
              head.lkg_generation,attempt.request_sha256 attempt_request_sha256
            FROM edge_apply_attempts attempt
            JOIN edge_acks ack ON ack.edge_apply_attempt_id=attempt.edge_apply_attempt_id
            JOIN edge_desired_state_heads head
              ON head.edge_deployment_id=attempt.edge_deployment_id
            WHERE attempt.attempt_nonce_hmac=?
            """,
            (nonce_hmac,),
        ).fetchone()
        if existing is not None:
            if not hmac.compare_digest(str(existing["attempt_request_sha256"]), request_sha256):
                raise ApiError("edge.idempotency_conflict")
            connection.commit()
            return _ack_response(existing, replay=True, request_id=request_id, trace_id=trace_id)
        snapshot = connection.execute(
            """
            SELECT snapshot.*,desired.rollback_of_generation
            FROM edge_signed_snapshots snapshot
            JOIN edge_desired_states desired
              ON desired.edge_desired_state_id=snapshot.edge_desired_state_id
            WHERE snapshot.workspace_id=? AND snapshot.tenant_id=?
              AND snapshot.edge_deployment_id=? AND snapshot.edge_signed_snapshot_id=?
            """,
            (
                agent["workspace_id"],
                agent["tenant_id"],
                deployment_id,
                payload.signed_snapshot_id,
            ),
        ).fetchone()
        if (
            snapshot is None
            or int(snapshot["generation"]) != payload.generation
            or not hmac.compare_digest(str(snapshot["envelope_sha256"]), payload.envelope_sha256)
        ):
            raise ApiError("edge.state_conflict")
        head = connection.execute(
            "SELECT * FROM edge_desired_state_heads WHERE edge_deployment_id=?",
            (deployment_id,),
        ).fetchone()
        if head is None or payload.generation > int(head["generation"]):
            raise ApiError("edge.state_conflict")
        _validate_lkg_reference(connection, deployment_id, payload)
        disposition = "accepted" if payload.generation == int(head["generation"]) else "late"
        if (
            disposition == "accepted"
            and payload.status == "failed"
            and (
                payload.lkg_generation != int(head["lkg_generation"])
                or payload.lkg_envelope_sha256 != head["lkg_envelope_sha256"]
            )
        ):
            raise ApiError("edge.state_conflict")
        now = _db_time(_utcnow())
        attempt_id = _id("eap_")
        ack_id = _id("eak_")
        connection.execute(
            """
            INSERT INTO edge_apply_attempts(
              edge_apply_attempt_id,workspace_id,tenant_id,edge_deployment_id,
              edge_signed_snapshot_id,generation,status,reason_code,apply_duration_ms,
              reported_lkg_generation,reported_lkg_sha256,attempt_nonce_hmac,request_sha256,
              occurred_at,recorded_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                attempt_id,
                agent["workspace_id"],
                agent["tenant_id"],
                deployment_id,
                payload.signed_snapshot_id,
                payload.generation,
                payload.status,
                payload.reason_code,
                payload.apply_duration_ms,
                payload.lkg_generation,
                payload.lkg_envelope_sha256,
                nonce_hmac,
                request_sha256,
                now,
                now,
            ),
        )
        connection.execute(
            """
            INSERT INTO edge_acks(
              edge_ack_id,workspace_id,tenant_id,edge_deployment_id,edge_apply_attempt_id,
              edge_signed_snapshot_id,generation,envelope_sha256,ack_status,disposition,
              request_sha256,acked_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                ack_id,
                agent["workspace_id"],
                agent["tenant_id"],
                deployment_id,
                attempt_id,
                payload.signed_snapshot_id,
                payload.generation,
                payload.envelope_sha256,
                payload.status,
                disposition,
                request_sha256,
                now,
            ),
        )
        if payload.lkg_generation > 0:
            connection.execute(
                """
                INSERT INTO edge_lkg_observations(
                  edge_lkg_observation_id,workspace_id,tenant_id,edge_deployment_id,
                  edge_ack_id,lkg_generation,lkg_envelope_sha256,observed_at)
                VALUES(?,?,?,?,?,?,?,?)
                """,
                (
                    _id("elg_"),
                    agent["workspace_id"],
                    agent["tenant_id"],
                    deployment_id,
                    ack_id,
                    payload.lkg_generation,
                    payload.lkg_envelope_sha256,
                    now,
                ),
            )
        if disposition == "accepted":
            changed = _changed_time(_utcnow(), head["updated_at"])
            if payload.status == "applied":
                connection.execute(
                    """
                    UPDATE edge_desired_state_heads
                    SET applied_generation=?,applied_envelope_sha256=?,lkg_generation=?,
                      lkg_envelope_sha256=?,last_ack_status='applied',last_reason_code='applied',
                      revision=revision+1,last_ack_at=?,updated_at=?
                    WHERE edge_desired_state_head_id=?
                    """,
                    (
                        payload.generation,
                        payload.envelope_sha256,
                        payload.lkg_generation,
                        payload.lkg_envelope_sha256,
                        changed,
                        changed,
                        head["edge_desired_state_head_id"],
                    ),
                )
                rollback_completed = snapshot["rollback_of_generation"] is not None
                _event(
                    connection,
                    row=snapshot,
                    actor_type="agent",
                    actor_id=str(agent["edge_session_id"]),
                    event_type=(
                        "edge.rollback_completed" if rollback_completed else "edge.state_converged"
                    ),
                    outcome="succeeded",
                    reason="rollback_applied" if rollback_completed else "applied",
                    envelope_sha256=payload.envelope_sha256,
                    request_id=request_id,
                    trace_id=trace_id,
                    now=changed,
                )
            else:
                connection.execute(
                    """
                    UPDATE edge_desired_state_heads
                    SET last_ack_status='failed',last_reason_code=?,revision=revision+1,
                      last_ack_at=?,updated_at=? WHERE edge_desired_state_head_id=?
                    """,
                    (
                        payload.reason_code,
                        changed,
                        changed,
                        head["edge_desired_state_head_id"],
                    ),
                )
                _event(
                    connection,
                    row=snapshot,
                    actor_type="agent",
                    actor_id=str(agent["edge_session_id"]),
                    event_type="edge.apply_failed",
                    outcome="failed",
                    reason=payload.reason_code,
                    envelope_sha256=payload.envelope_sha256,
                    request_id=request_id,
                    trace_id=trace_id,
                    now=changed,
                )
        result = connection.execute(
            """
            SELECT ack.*,head.generation desired_generation,head.applied_generation,
              head.lkg_generation FROM edge_acks ack
            JOIN edge_desired_state_heads head ON head.edge_deployment_id=ack.edge_deployment_id
            WHERE ack.edge_ack_id=?
            """,
            (ack_id,),
        ).fetchone()
        connection.commit()
        return _ack_response(result, replay=False, request_id=request_id, trace_id=trace_id)
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, DbError, KeyError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("edge.unavailable") from None
    finally:
        connection.close()


def _request_publish(
    connection: sqlite3.Connection,
    config: AppConfig,
    context: WorkspaceSessionContext,
    deployment: sqlite3.Row,
    payload: EdgeDesiredStatePublishRequest,
    *,
    expected_revision: int,
    request_id: str,
    trace_id: str,
) -> str:
    deployment_id = str(deployment["edge_deployment_id"])
    _ensure_no_pending(connection, deployment_id)
    head = connection.execute(
        "SELECT * FROM edge_desired_state_heads WHERE edge_deployment_id=?",
        (deployment_id,),
    ).fetchone()
    current_revision = int(head["revision"]) if head is not None else 0
    if expected_revision != current_revision:
        raise ApiError("edge.revision_conflict")
    source = connection.execute(
        """
        SELECT snapshot.* FROM config_snapshots snapshot
        JOIN config_snapshot_heads head
          ON head.config_snapshot_id=snapshot.config_snapshot_id
         AND head.workspace_id=snapshot.workspace_id AND head.tenant_id=snapshot.tenant_id
        WHERE snapshot.workspace_id=? AND snapshot.tenant_id=?
          AND snapshot.config_snapshot_id=?
        """,
        (context.workspace_id, context.tenant_id, payload.source_config_snapshot_id),
    ).fetchone()
    if source is None:
        raise ApiError("edge.state_conflict")
    previous = connection.execute(
        """
        SELECT source_revocation_watermark FROM edge_desired_states
        WHERE edge_deployment_id=? AND status IN('published','superseded')
        ORDER BY generation DESC LIMIT 1
        """,
        (deployment_id,),
    ).fetchone()
    previous_watermark = int(previous[0]) if previous is not None else 0
    high_risk = int(source["revocation_watermark"]) > previous_watermark
    generation = _next_generation(connection, deployment_id)
    base_generation = int(head["generation"]) if head is not None else 0
    now = _db_time(_utcnow())
    state_id = _id("edst_")
    status = "approval_pending" if high_risk else "published"
    published_at = None if high_risk else now
    request_sha256 = _request_digest(payload.model_dump(mode="json"))
    connection.execute(
        """
        INSERT INTO edge_desired_states(
          edge_desired_state_id,workspace_id,tenant_id,edge_deployment_id,generation,
          base_generation,rollback_of_generation,source_config_snapshot_id,source_generation,
          source_manifest_sha256,source_revocation_watermark,change_class,status,
          requested_by_account_id,approved_by_account_id,revision,request_sha256,requested_at,
          decided_at,published_at,updated_at)
        VALUES(?,?,?,?,?,?,NULL,?,?,?,?,?,?,?,NULL,1,?,?,NULL,?,?)
        """,
        (
            state_id,
            context.workspace_id,
            context.tenant_id,
            deployment_id,
            generation,
            base_generation,
            source["config_snapshot_id"],
            source["generation"],
            source["manifest_sha256"],
            source["revocation_watermark"],
            "high_risk" if high_risk else "standard",
            status,
            context.account_id,
            request_sha256,
            now,
            published_at,
            now,
        ),
    )
    if not high_risk:
        _materialize(
            connection,
            config,
            context,
            deployment,
            state_id,
            content_override=None,
            event_reason="published",
            request_id=request_id,
            trace_id=trace_id,
        )
    return state_id


def _decide_pending_state(
    connection: sqlite3.Connection,
    config: AppConfig,
    context: WorkspaceSessionContext,
    deployment: sqlite3.Row,
    payload: EdgeDesiredStateApprovalRequest,
    *,
    expected_revision: int,
    request_id: str,
    trace_id: str,
) -> str:
    if context.membership_role not in {"owner", "admin"}:
        raise ApiError("workspace_session.permission_denied")
    row = connection.execute(
        """
        SELECT * FROM edge_desired_states
        WHERE workspace_id=? AND tenant_id=? AND edge_deployment_id=?
          AND edge_desired_state_id=?
        """,
        (
            context.workspace_id,
            context.tenant_id,
            deployment["edge_deployment_id"],
            payload.desired_state_id,
        ),
    ).fetchone()
    if row is None or str(row["status"]) != "approval_pending":
        raise ApiError("edge.state_conflict")
    if int(row["revision"]) != expected_revision or hmac.compare_digest(
        str(row["requested_by_account_id"]), context.account_id
    ):
        raise ApiError("edge.revision_conflict")
    if payload.action == "approve" and payload.reason_code != "approved_change":
        raise ApiError("edge.request_invalid")
    if payload.action == "reject" and payload.reason_code == "approved_change":
        raise ApiError("edge.request_invalid")
    now = _changed_time(_utcnow(), row["updated_at"])
    status = "published" if payload.action == "approve" else "rejected"
    connection.execute(
        """
        UPDATE edge_desired_states SET status=?,approved_by_account_id=?,revision=revision+1,
          decided_at=?,published_at=?,updated_at=? WHERE edge_desired_state_id=?
        """,
        (
            status,
            context.account_id,
            now,
            now if payload.action == "approve" else None,
            now,
            payload.desired_state_id,
        ),
    )
    connection.execute(
        """
        INSERT INTO edge_desired_state_approvals(
          edge_desired_state_approval_id,workspace_id,tenant_id,edge_deployment_id,
          edge_desired_state_id,requested_by_account_id,decided_by_account_id,decision,
          reason_code,desired_state_revision,request_id,trace_id,decided_at)
        VALUES(?,?,?,?,?,?,?,?,?,2,?,?,?)
        """,
        (
            _id("edsa_"),
            context.workspace_id,
            context.tenant_id,
            deployment["edge_deployment_id"],
            payload.desired_state_id,
            row["requested_by_account_id"],
            context.account_id,
            payload.action,
            payload.reason_code,
            _identifier(request_id),
            _identifier(trace_id),
            now,
        ),
    )
    if payload.action == "approve":
        _materialize(
            connection,
            config,
            context,
            deployment,
            payload.desired_state_id,
            content_override=None,
            event_reason="approved",
            request_id=request_id,
            trace_id=trace_id,
        )
    return payload.desired_state_id


def _materialize(
    connection: sqlite3.Connection,
    config: AppConfig,
    context: WorkspaceSessionContext,
    deployment: sqlite3.Row,
    state_id: str,
    *,
    content_override: dict[str, object] | None,
    event_reason: str,
    request_id: str,
    trace_id: str,
) -> None:
    state = connection.execute(
        "SELECT * FROM edge_desired_states WHERE edge_desired_state_id=?",
        (state_id,),
    ).fetchone()
    if state is None or str(state["status"]) != "published":
        raise ApiError("edge.state_conflict")
    source = connection.execute(
        "SELECT * FROM config_snapshots WHERE config_snapshot_id=? AND workspace_id=? "
        "AND tenant_id=?",
        (
            state["source_config_snapshot_id"],
            context.workspace_id,
            context.tenant_id,
        ),
    ).fetchone()
    if source is None:
        raise ApiError("edge.state_conflict")
    if content_override is None:
        source_manifest = open_config_value(
            config,
            str(source["manifest_envelope"]),
            expected_sha256=str(source["manifest_sha256"]),
            owner_id=str(source["config_snapshot_id"]),
            workspace_id=context.workspace_id,
            tenant_id=context.tenant_id,
            kind="manifest",
        )
        content: dict[str, object] = {
            "schema_version": 1,
            "deployment": {
                "deployment_id": str(deployment["edge_deployment_id"]),
                "mode": str(deployment["mode"]),
                "region_code": str(deployment["region_code"]),
                "revision": int(deployment["revision"]),
            },
            "source_manifest": source_manifest,
        }
    else:
        content = content_override
    content_bytes = canonical_json_bytes(content)
    content_sha256 = hashlib.sha256(content_bytes).hexdigest()
    private_key, kid, trust_id = _signing_material(connection, config, now=_utcnow())
    now_dt = _utcnow()
    now = _db_time(now_dt)
    artifact_id = _id("esa_")
    snapshot_id = _id("edsn_")
    unsigned = {
        "domain": "metahub.desired-state.v1",
        "protocol_version": 1,
        "snapshot_id": snapshot_id,
        "artifact_id": artifact_id,
        "workspace_id": context.workspace_id,
        "tenant_id": context.tenant_id,
        "deployment_id": str(deployment["edge_deployment_id"]),
        "generation": int(state["generation"]),
        "base_generation": int(state["base_generation"]),
        "revocation_generation": int(deployment["revocation_generation"]),
        "issued_at": now,
        "not_before": now,
        "security_expires_at": _db_time(now_dt + _SECURITY_TTL),
        "expires_at": _db_time(now_dt + _CONFIGURATION_TTL),
        "alg": "Ed25519",
        "kid": kid,
        "content_sha256": content_sha256,
        "content": content,
    }
    signed = sign_desired_state(unsigned, private_key)
    envelope_bytes = canonical_json_bytes(signed, maximum=786_432)
    envelope_sha256 = hashlib.sha256(envelope_bytes).hexdigest()
    connection.execute(
        """
        INSERT INTO edge_snapshot_artifacts(
          edge_snapshot_artifact_id,workspace_id,tenant_id,edge_deployment_id,
          edge_desired_state_id,generation,content_rfc8785,content_sha256,
          content_size_bytes,created_at)
        VALUES(?,?,?,?,?,?,?,?,?,?)
        """,
        (
            artifact_id,
            context.workspace_id,
            context.tenant_id,
            deployment["edge_deployment_id"],
            state_id,
            state["generation"],
            content_bytes.decode("utf-8"),
            content_sha256,
            len(content_bytes),
            now,
        ),
    )
    connection.execute(
        """
        INSERT INTO edge_signed_snapshots(
          edge_signed_snapshot_id,workspace_id,tenant_id,edge_deployment_id,
          edge_desired_state_id,edge_snapshot_artifact_id,edge_trust_manifest_id,
          generation,base_generation,revocation_generation,domain_separator,protocol_version,
          algorithm,kid,envelope_rfc8785,envelope_sha256,content_sha256,signature_b64,
          issued_at,not_before,security_expires_at,expires_at,created_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,'metahub.desired-state.v1',1,'Ed25519',?,?,?,?,?,?,?,?,?,?)
        """,
        (
            snapshot_id,
            context.workspace_id,
            context.tenant_id,
            deployment["edge_deployment_id"],
            state_id,
            artifact_id,
            trust_id,
            state["generation"],
            state["base_generation"],
            deployment["revocation_generation"],
            kid,
            envelope_bytes.decode("utf-8"),
            envelope_sha256,
            content_sha256,
            signed["signature"],
            now,
            now,
            unsigned["security_expires_at"],
            unsigned["expires_at"],
            now,
        ),
    )
    head = connection.execute(
        "SELECT * FROM edge_desired_state_heads WHERE edge_deployment_id=?",
        (deployment["edge_deployment_id"],),
    ).fetchone()
    if head is None:
        connection.execute(
            """
            INSERT INTO edge_desired_state_heads(
              edge_desired_state_head_id,workspace_id,tenant_id,edge_deployment_id,generation,
              edge_desired_state_id,edge_snapshot_artifact_id,edge_signed_snapshot_id,
              desired_envelope_sha256,applied_generation,applied_envelope_sha256,
              lkg_generation,lkg_envelope_sha256,last_ack_status,last_reason_code,revision,
              published_at,last_ack_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,0,NULL,0,NULL,'not_observed','not_observed',1,?,NULL,?)
            """,
            (
                _id("edsh_"),
                context.workspace_id,
                context.tenant_id,
                deployment["edge_deployment_id"],
                state["generation"],
                state_id,
                artifact_id,
                snapshot_id,
                envelope_sha256,
                now,
                now,
            ),
        )
    else:
        if int(state["base_generation"]) != int(head["generation"]):
            raise ApiError("edge.state_conflict")
        changed = _changed_time(now_dt, head["updated_at"])
        connection.execute(
            "UPDATE edge_desired_states SET status='superseded',revision=revision+1,updated_at=? "
            "WHERE edge_desired_state_id=? AND status='published'",
            (changed, head["edge_desired_state_id"]),
        )
        connection.execute(
            """
            UPDATE edge_desired_state_heads SET generation=?,edge_desired_state_id=?,
              edge_snapshot_artifact_id=?,edge_signed_snapshot_id=?,desired_envelope_sha256=?,
              last_ack_status='not_observed',last_reason_code='not_observed',revision=revision+1,
              published_at=?,last_ack_at=NULL,updated_at=?
            WHERE edge_desired_state_head_id=? AND generation=?
            """,
            (
                state["generation"],
                state_id,
                artifact_id,
                snapshot_id,
                envelope_sha256,
                changed,
                changed,
                head["edge_desired_state_head_id"],
                head["generation"],
            ),
        )
    event_row = dict(state)
    event_row["envelope_sha256"] = envelope_sha256
    _event(
        connection,
        row=event_row,
        actor_type="human",
        actor_id=context.account_id,
        event_type="edge.desired_state_published",
        outcome="succeeded",
        reason=event_reason,
        envelope_sha256=envelope_sha256,
        request_id=request_id,
        trace_id=trace_id,
        now=now,
    )


def _signing_material(
    connection: sqlite3.Connection, config: AppConfig, *, now: datetime
) -> tuple[Ed25519PrivateKey, str, str]:
    secret = config.edge_snapshot_signing_secret
    version = config.edge_snapshot_signing_version
    if secret is None or version < 1:
        raise ApiError("edge.unavailable")
    seed = hmac.new(
        secret.encode("utf-8"),
        f"metahub/edge-desired-state-signing/v1\0{version}".encode("ascii"),
        hashlib.sha256,
    ).digest()
    private_key = Ed25519PrivateKey.from_private_bytes(seed)
    kid = f"edge-ds-key-v{version}"
    public_b64 = public_key_b64(private_key)
    public_raw = private_key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    public_sha256 = hashlib.sha256(public_raw).hexdigest()
    now_db = _db_time(now)
    root = connection.execute(
        "SELECT * FROM edge_snapshot_signing_roots WHERE kid=?", (kid,)
    ).fetchone()
    if root is None:
        connection.execute(
            """
            INSERT INTO edge_snapshot_signing_roots(
              kid,algorithm,public_key_b64,public_key_sha256,created_at)
            VALUES(?,'Ed25519',?,?,?)
            """,
            (kid, public_b64, public_sha256, now_db),
        )
    elif not (
        hmac.compare_digest(str(root["public_key_b64"]), public_b64)
        and hmac.compare_digest(str(root["public_key_sha256"]), public_sha256)
    ):
        raise ApiError("edge.unavailable")
    head = connection.execute(
        """
        SELECT head.*,manifest.manifest_rfc8785,manifest.expires_at
        FROM edge_trust_manifest_heads head
        JOIN edge_trust_manifests manifest
          ON manifest.edge_trust_manifest_id=head.edge_trust_manifest_id
        WHERE head.singleton=1
        """
    ).fetchone()
    if head is not None:
        document = strict_json_object(str(head["manifest_rfc8785"]).encode("utf-8"), canonical=True)
        current = document.get("current")
        if (
            type(current) is not dict
            or document.get("signer_kid") != kid
            or current.get("kid") != kid
            or current.get("public_key") != public_b64
        ):
            raise ApiError("edge.unavailable")
        if _datetime(head["expires_at"]) > now + _TRUST_REFRESH_WINDOW:
            return private_key, kid, str(head["edge_trust_manifest_id"])
        generation = int(head["generation"]) + 1
    else:
        generation = 1
    unsigned = {
        "domain": "metahub.edge-trust-manifest.v1",
        "protocol_version": 1,
        "generation": generation,
        "issued_at": now_db,
        "expires_at": _db_time(now + _TRUST_TTL),
        "signer_kid": kid,
        "current": {"alg": "Ed25519", "kid": kid, "public_key": public_b64},
        "next": None,
        "revoked_kids": [],
    }
    signed = sign_trust_manifest(unsigned, private_key)
    manifest_bytes = canonical_json_bytes(signed)
    manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
    manifest_id = _id("etmf_")
    connection.execute(
        """
        INSERT INTO edge_trust_manifests(
          edge_trust_manifest_id,generation,signer_kid,current_kid,next_kid,
          manifest_rfc8785,manifest_sha256,issued_at,expires_at,created_at)
        VALUES(?,?,?,?,NULL,?,?,?,?,?)
        """,
        (
            manifest_id,
            generation,
            kid,
            kid,
            manifest_bytes.decode("utf-8"),
            manifest_sha256,
            now_db,
            unsigned["expires_at"],
            now_db,
        ),
    )
    if head is None:
        connection.execute(
            """
            INSERT INTO edge_trust_manifest_heads(
              edge_trust_manifest_head_id,singleton,generation,edge_trust_manifest_id,
              manifest_sha256,revision,updated_at)
            VALUES(?,1,?,?,?,1,?)
            """,
            (_id("etmh_"), generation, manifest_id, manifest_sha256, now_db),
        )
    else:
        changed = _changed_time(now, head["updated_at"])
        connection.execute(
            """
            UPDATE edge_trust_manifest_heads SET generation=?,edge_trust_manifest_id=?,
              manifest_sha256=?,revision=revision+1,updated_at=?
            WHERE edge_trust_manifest_head_id=?
            """,
            (
                generation,
                manifest_id,
                manifest_sha256,
                changed,
                head["edge_trust_manifest_head_id"],
            ),
        )
    return private_key, kid, manifest_id


def _authenticated_agent(
    connection: sqlite3.Connection,
    config: AppConfig,
    *,
    deployment_id: str,
    verified_fingerprint: str,
    edge_session_token: str,
) -> sqlite3.Row:
    if (
        _SESSION_RE.fullmatch(edge_session_token) is None
        or _HASH_RE.fullmatch(verified_fingerprint) is None
    ):
        raise ApiError("edge.authentication_failed")
    token_hmac = _secret_hmac(config, "edge-session-token-v1", edge_session_token)
    row = connection.execute(
        """
        SELECT session.*,deployment.status deployment_status,
          deployment.revocation_generation current_revocation_generation,
          identity.status identity_status,identity.capability_codes_json,
          certificate.status certificate_status,
          certificate.not_after
        FROM edge_sessions session
        JOIN edge_deployments deployment
          ON deployment.edge_deployment_id=session.edge_deployment_id
        JOIN edge_device_identities identity
          ON identity.edge_device_identity_id=session.edge_device_identity_id
        JOIN edge_device_certificates certificate
          ON certificate.edge_device_certificate_id=session.edge_device_certificate_id
        WHERE session.edge_deployment_id=? AND session.session_token_hmac=?
          AND session.peer_binding_sha256=? AND session.status='active'
          AND session.expires_at>strftime('%Y-%m-%dT%H:%M:%fZ','now')
        """,
        (deployment_id, token_hmac, verified_fingerprint),
    ).fetchone()
    if (
        row is None
        or str(row["deployment_status"]) != "active"
        or int(row["revocation_generation"]) != int(row["current_revocation_generation"])
        or str(row["identity_status"]) not in {"active", "superseded"}
        or str(row["certificate_status"]) not in {"active", "draining"}
        or _datetime(row["not_after"]) <= _utcnow()
    ):
        raise ApiError("edge.authentication_failed")
    return row


def _validate_ack(payload: EdgeAckCreateRequest) -> None:
    if payload.status == "applied":
        if (
            payload.reason_code != "applied"
            or payload.lkg_generation != payload.generation
            or payload.lkg_envelope_sha256 != payload.envelope_sha256
        ):
            raise ApiError("edge.request_invalid")
    elif payload.reason_code == "applied":
        raise ApiError("edge.request_invalid")
    if (payload.lkg_generation == 0) != (payload.lkg_envelope_sha256 is None):
        raise ApiError("edge.request_invalid")


def _validate_lkg_reference(
    connection: sqlite3.Connection,
    deployment_id: str,
    payload: EdgeAckCreateRequest,
) -> None:
    if payload.lkg_generation == 0:
        return
    row = connection.execute(
        "SELECT envelope_sha256 FROM edge_signed_snapshots "
        "WHERE edge_deployment_id=? AND generation=?",
        (deployment_id, payload.lkg_generation),
    ).fetchone()
    if row is None or not hmac.compare_digest(
        str(row["envelope_sha256"]), str(payload.lkg_envelope_sha256)
    ):
        raise ApiError("edge.state_conflict")


def _ack_response(
    row: sqlite3.Row,
    *,
    replay: bool,
    request_id: str,
    trace_id: str,
) -> EdgeAckResponse:
    return EdgeAckResponse(
        ack_id=str(row["edge_ack_id"]),
        disposition=str(row["disposition"]),
        status=str(row["ack_status"]),
        desired_generation=int(row["desired_generation"]),
        applied_generation=int(row["applied_generation"]),
        lkg_generation=int(row["lkg_generation"]),
        idempotent_replay=replay,
        request_id=_identifier(request_id),
        trace_id=_identifier(trace_id),
    )


def _latest_state(
    connection: sqlite3.Connection,
    context: WorkspaceSessionContext,
    deployment_id: str,
) -> EdgeDesiredStateSummary | None:
    row = connection.execute(
        """
        SELECT desired.*,head.revision head_revision,head.generation desired_generation,
          head.applied_generation,
          head.lkg_generation,head.last_ack_status,head.last_reason_code,head.last_ack_at
        FROM edge_desired_states desired
        LEFT JOIN edge_desired_state_heads head
          ON head.edge_deployment_id=desired.edge_deployment_id
        WHERE desired.workspace_id=? AND desired.tenant_id=?
          AND desired.edge_deployment_id=?
        ORDER BY desired.generation DESC LIMIT 1
        """,
        (context.workspace_id, context.tenant_id, deployment_id),
    ).fetchone()
    return _state_summary(row) if row is not None else None


def _state_by_id(
    connection: sqlite3.Connection,
    context: WorkspaceSessionContext,
    deployment_id: str,
    state_id: str,
) -> EdgeDesiredStateSummary:
    row = connection.execute(
        """
        SELECT desired.*,head.revision head_revision,head.generation desired_generation,
          head.applied_generation,
          head.lkg_generation,head.last_ack_status,head.last_reason_code,head.last_ack_at
        FROM edge_desired_states desired
        LEFT JOIN edge_desired_state_heads head
          ON head.edge_deployment_id=desired.edge_deployment_id
        WHERE desired.workspace_id=? AND desired.tenant_id=?
          AND desired.edge_deployment_id=? AND desired.edge_desired_state_id=?
        """,
        (context.workspace_id, context.tenant_id, deployment_id, state_id),
    ).fetchone()
    if row is None:
        raise ApiError("edge.deployment_not_found")
    return _state_summary(row)


def _state_summary(row: sqlite3.Row) -> EdgeDesiredStateSummary:
    desired_generation = int(row["desired_generation"] or 0)
    applied_generation = int(row["applied_generation"] or 0)
    return EdgeDesiredStateSummary(
        desired_state_id=str(row["edge_desired_state_id"]),
        deployment_id=str(row["edge_deployment_id"]),
        generation=int(row["generation"]),
        base_generation=int(row["base_generation"]),
        rollback_of_generation=(
            int(row["rollback_of_generation"])
            if row["rollback_of_generation"] is not None
            else None
        ),
        source_config_snapshot_id=str(row["source_config_snapshot_id"]),
        source_generation=int(row["source_generation"]),
        source_revocation_watermark=int(row["source_revocation_watermark"]),
        change_class=str(row["change_class"]),
        status=str(row["status"]),
        revision=int(row["revision"]),
        head_revision=int(row["head_revision"] or 0),
        requested_by_account_id=str(row["requested_by_account_id"]),
        approved_by_account_id=(
            str(row["approved_by_account_id"])
            if row["approved_by_account_id"] is not None
            else None
        ),
        desired_generation=desired_generation,
        applied_generation=applied_generation,
        lkg_generation=int(row["lkg_generation"] or 0),
        last_ack_status=str(row["last_ack_status"] or "not_observed"),
        last_reason_code=str(row["last_reason_code"] or "not_observed"),
        drifted=desired_generation != applied_generation,
        requested_at=_datetime(row["requested_at"]),
        decided_at=_optional_datetime(row["decided_at"]),
        published_at=_optional_datetime(row["published_at"]),
        last_ack_at=_optional_datetime(row["last_ack_at"]),
        updated_at=_datetime(row["updated_at"]),
    )


def _event(
    connection: sqlite3.Connection,
    *,
    row,
    actor_type: str,
    actor_id: str | None,
    event_type: str,
    outcome: str,
    reason: str,
    envelope_sha256: str | None,
    request_id: str,
    trace_id: str,
    now: str,
) -> None:
    event_id = _id("ede_")
    connection.execute(
        """
        INSERT INTO edge_desired_state_events(
          edge_desired_state_event_id,workspace_id,tenant_id,edge_deployment_id,
          edge_desired_state_id,actor_type,actor_id,event_type,generation,outcome_code,
          reason_code,envelope_sha256,request_id,trace_id,occurred_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            event_id,
            row["workspace_id"],
            row["tenant_id"],
            row["edge_deployment_id"],
            row["edge_desired_state_id"],
            actor_type,
            actor_id,
            event_type,
            row["generation"],
            outcome,
            reason,
            envelope_sha256,
            _identifier(request_id),
            _identifier(trace_id),
            now,
        ),
    )
    domain = "security" if event_type == "edge.apply_failed" else "fleet"
    connection.execute(
        """
        INSERT INTO edge_desired_state_event_outbox(
          edge_desired_state_outbox_id,edge_desired_state_event_id,delivery_domain,status,
          attempt_count,available_at,delivered_at,last_error_code,created_at,updated_at)
        VALUES(?,? ,?,'pending',0,?,NULL,NULL,?,?)
        """,
        (_id("edo_"), event_id, domain, now, now, now),
    )


def _command(
    connection: sqlite3.Connection,
    context: WorkspaceSessionContext,
    action: str,
    idempotency_sha256: str,
) -> sqlite3.Row | None:
    return connection.execute(
        """
        SELECT * FROM workspace_edge_state_commands
        WHERE workspace_id=? AND tenant_id=? AND actor_account_id=?
          AND action_code=? AND idempotency_sha256=?
        """,
        (
            context.workspace_id,
            context.tenant_id,
            context.account_id,
            action,
            idempotency_sha256,
        ),
    ).fetchone()


def _insert_command(
    connection: sqlite3.Connection,
    context: WorkspaceSessionContext,
    *,
    deployment_id: str,
    action: str,
    idempotency_sha256: str,
    request_sha256: str,
    result_desired_state_id: str,
    now: str,
) -> None:
    connection.execute(
        """
        INSERT INTO workspace_edge_state_commands(
          workspace_edge_state_command_id,workspace_id,tenant_id,edge_deployment_id,
          actor_account_id,action_code,idempotency_sha256,request_sha256,
          result_desired_state_id,created_at)
        VALUES(?,?,?,?,?,?,?,?,?,?)
        """,
        (
            _id("edcm_"),
            context.workspace_id,
            context.tenant_id,
            deployment_id,
            context.account_id,
            action,
            idempotency_sha256,
            request_sha256,
            result_desired_state_id,
            now,
        ),
    )


def _workspace_idempotency(
    config: AppConfig,
    context: WorkspaceSessionContext,
    action: str,
    value: str,
) -> str:
    return hmac.new(
        config.workspace_session_secret.encode("utf-8"),
        (
            "prd121/workspace-idempotency\0"
            f"{context.workspace_id}\0{context.account_id}\0{action}\0{value}"
        ).encode(),
        hashlib.sha256,
    ).hexdigest()


def _ensure_no_pending(connection: sqlite3.Connection, deployment_id: str) -> None:
    if (
        connection.execute(
            "SELECT 1 FROM edge_desired_states WHERE edge_deployment_id=? "
            "AND status='approval_pending'",
            (deployment_id,),
        ).fetchone()
        is not None
    ):
        raise ApiError("edge.state_conflict")


def _next_generation(connection: sqlite3.Connection, deployment_id: str) -> int:
    row = connection.execute(
        "SELECT coalesce(max(generation),0)+1 FROM edge_desired_states WHERE edge_deployment_id=?",
        (deployment_id,),
    ).fetchone()
    return int(row[0])


def _request_digest(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _changed_time(value: datetime, previous: object) -> str:
    checked = value.astimezone(UTC)
    prior = _datetime(previous)
    if checked <= prior:
        checked = prior + timedelta(milliseconds=1)
    return _db_time(checked)


def _deployment_id(value: str) -> None:
    if _DEPLOYMENT_RE.fullmatch(value) is None:
        raise ApiError("edge.deployment_not_found")


def _idempotency_key(value: str) -> None:
    if _IDEMPOTENCY_RE.fullmatch(value) is None:
        raise ApiError("edge.request_invalid")


__all__ = [
    "acknowledge_agent_desired_state",
    "get_workspace_desired_state",
    "pull_agent_desired_state",
    "rollback_workspace_desired_state",
    "write_workspace_desired_state",
]
