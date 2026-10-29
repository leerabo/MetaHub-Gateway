"""License issuance, refresh, revocation, and Agent synchronization for PRD-128."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from metahub_shared.db.errors import DbError
from metahub_shared.private_gateway import (
    LicenseCapacity,
    LicenseContractError,
    LicenseEntitlement,
    SignedEntitlement,
    canonical_edge_json_bytes,
    sign_license_entitlement,
)
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError

from metahub_control.platform_access.service import PlatformContext

from .license_models import (
    AgentLicenseAcknowledgementRequest,
    AgentLicenseAcknowledgementResponse,
    AgentLicenseResponse,
    LicenseAcknowledgementSummary,
    LicenseListResponse,
    LicenseResponse,
    LicenseSummary,
    LicenseUsageSummary,
    PlatformLicenseIssueRequest,
    PlatformLicenseRevokeRequest,
    WorkspaceLicenseRefreshRequest,
)
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
from .upgrade_service import _authenticated_agent

_IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$")
_MAX_LICENSE_LIFETIME = timedelta(days=366)
_DEFAULT_REFRESH_LIFETIME = timedelta(days=30)
_MIN_LICENSE_LIFETIME = timedelta(hours=1)
_CLOCK_SKEW = timedelta(minutes=5)


@dataclass(frozen=True, slots=True)
class _CommercialSource:
    snapshot_id: str
    snapshot_version: int
    order_id: str | None
    source_type: str
    source_id: str
    effective_end: datetime | None


def issue_platform_license(
    config: AppConfig,
    context: PlatformContext,
    payload: PlatformLicenseIssueRequest,
    *,
    idempotency_key: str,
    request_id: str,
    trace_id: str,
) -> LicenseResponse:
    """Issue a first entitlement or atomically expand an existing active license."""

    _require_platform(context, "platform.edge_license.issue")
    action = "edge.license.issue"
    request_sha256 = _digest(payload.model_dump(mode="json"))
    idempotency_sha256 = _platform_idempotency(
        config, context.operator_id, action, idempotency_key
    )
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        replay = _action_replay(
            connection, "platform", context.operator_id, action, idempotency_sha256
        )
        if replay is not None:
            _verify_replay(replay, request_sha256)
            row = _license_row(connection, str(replay["result_object_id"]))
            result = _response(connection, row, request_id, trace_id, replay=True)
            connection.commit()
            return result

        deployment = connection.execute(
            "SELECT * FROM edge_deployments WHERE edge_deployment_id=?",
            (payload.deployment_id,),
        ).fetchone()
        if deployment is None or str(deployment["status"]) != "active":
            raise ApiError("edge_license.deployment_ineligible")
        source = _commercial_source(
            connection,
            workspace_id=str(deployment["workspace_id"]),
            tenant_id=str(deployment["tenant_id"]),
            requested_order_id=payload.source_order_id,
            now=_utcnow(),
        )
        now_value = _utcnow()
        expires_at = _aware(payload.expires_at)
        _validate_expiry(now_value, expires_at, source.effective_end)
        capacity = LicenseCapacity(
            node_limit=payload.node_limit,
            concurrent_request_limit=payload.concurrent_request_limit,
            features=payload.features,
        )
        existing = connection.execute(
            "SELECT * FROM edge_licenses WHERE edge_deployment_id=?",
            (payload.deployment_id,),
        ).fetchone()
        if existing is not None and str(existing["status"]) == "revoked":
            raise ApiError("edge_license.revoked")
        if existing is None:
            if payload.expected_revision is not None:
                raise ApiError("edge_license.revision_conflict")
            license_id = _id("lic_")
            generation = 1
            refresh_counter = 1
            revocation_generation = 0
            now = _db_time(now_value)
            connection.execute(
                """
                INSERT INTO edge_licenses(
                  edge_license_id,workspace_id,tenant_id,edge_deployment_id,status,
                  current_entitlement_id,current_generation,refresh_counter,
                  revocation_generation,revision,issued_by_operator_id,
                  revoked_by_operator_id,revoked_reason_code,created_at,updated_at,revoked_at)
                VALUES(?,?,?,?,'provisioning',NULL,0,0,0,1,?,NULL,NULL,?,?,NULL)
                """,
                (
                    license_id,
                    deployment["workspace_id"],
                    deployment["tenant_id"],
                    payload.deployment_id,
                    context.operator_id,
                    now,
                    now,
                ),
            )
        else:
            if payload.expected_revision != int(existing["revision"]):
                raise ApiError("edge_license.revision_conflict")
            license_id = str(existing["edge_license_id"])
            generation = int(existing["current_generation"]) + 1
            refresh_counter = int(existing["refresh_counter"]) + 1
            revocation_generation = int(existing["revocation_generation"])

        device_generation = _device_generation(connection, payload.deployment_id)
        signed = _insert_entitlement(
            connection,
            config,
            license_id=license_id,
            workspace_id=str(deployment["workspace_id"]),
            tenant_id=str(deployment["tenant_id"]),
            deployment_id=payload.deployment_id,
            device_generation=device_generation,
            source=source,
            capacity=capacity,
            generation=generation,
            refresh_counter=refresh_counter,
            revocation_generation=revocation_generation,
            status="active",
            issued_at=now_value,
            expires_at=expires_at,
            grace_ends_at=expires_at + timedelta(days=payload.grace_days),
        )
        current = connection.execute(
            "SELECT * FROM edge_licenses WHERE edge_license_id=?", (license_id,)
        ).fetchone()
        if current is None:
            raise ApiError("edge_license.integrity_failed")
        updated_at = _changed_stamp(now_value, current["updated_at"])
        updated = connection.execute(
            """
            UPDATE edge_licenses SET status='active',current_entitlement_id=?,
              current_generation=?,refresh_counter=?,revocation_generation=?,
              revision=revision+1,updated_at=?
            WHERE edge_license_id=? AND status IN('provisioning','active') AND revision=?
            """,
            (
                signed.entitlement.entitlement_id,
                generation,
                refresh_counter,
                revocation_generation,
                updated_at,
                license_id,
                current["revision"],
            ),
        ).rowcount
        if updated != 1:
            raise ApiError("edge_license.revision_conflict")
        event_type = "license.issued" if existing is None else "license.refreshed"
        now = _db_time(now_value)
        _insert_action(
            connection,
            actor_domain="platform",
            actor_id=context.operator_id,
            action=action,
            idempotency_sha256=idempotency_sha256,
            request_sha256=request_sha256,
            result_object_id=license_id,
            now=now,
        )
        _license_event(
            connection,
            workspace_id=str(deployment["workspace_id"]),
            tenant_id=str(deployment["tenant_id"]),
            deployment_id=payload.deployment_id,
            license_id=license_id,
            entitlement_id=signed.entitlement.entitlement_id,
            event_type=event_type,
            reason_code=payload.reason_code,
            actor_domain="platform",
            actor_id=context.operator_id,
            request_id=request_id,
            trace_id=trace_id,
            now=now,
        )
        _platform_audit(
            connection,
            context,
            action=event_type,
            object_id=license_id,
            reason_code=payload.reason_code,
            request_id=request_id,
            trace_id=trace_id,
            after={
                "deployment_id": payload.deployment_id,
                "generation": generation,
                "manifest_sha256": signed.manifest_sha256,
            },
            now=now,
        )
        row = _license_row(connection, license_id)
        result = _response(connection, row, request_id, trace_id)
        connection.commit()
        return result
    except ApiError:
        _rollback(connection)
        raise
    except LicenseContractError:
        _rollback(connection)
        raise ApiError("edge_license.integrity_failed") from None
    except sqlite3.IntegrityError:
        _rollback(connection)
        raise ApiError("edge_license.state_conflict") from None
    except (DbError, sqlite3.Error, KeyError, OSError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("edge_license.unavailable") from None
    finally:
        connection.close()


def list_platform_licenses(
    config: AppConfig,
    context: PlatformContext,
    *,
    request_id: str,
    trace_id: str,
) -> LicenseListResponse:
    _require_platform(context, "platform.edge_license.read")
    connection = _connect(config)
    try:
        connection.execute("BEGIN")
        rows = connection.execute(
            "SELECT * FROM edge_licenses WHERE status IN('active','revoked') "
            "ORDER BY updated_at DESC,edge_license_id DESC LIMIT 200"
        ).fetchall()
        result = LicenseListResponse(
            licenses=tuple(_summary(connection, row) for row in rows),
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
        raise ApiError("edge_license.unavailable") from None
    finally:
        connection.close()


def revoke_platform_license(
    config: AppConfig,
    context: PlatformContext,
    license_id: str,
    payload: PlatformLicenseRevokeRequest,
    *,
    idempotency_key: str,
    request_id: str,
    trace_id: str,
) -> LicenseResponse:
    """Publish an explicit revocation entitlement; commercial grace never applies."""

    _require_platform(context, "platform.edge_license.revoke")
    action = "edge.license.revoke"
    request_sha256 = _digest(
        {"license_id": license_id, **payload.model_dump(mode="json")}
    )
    idempotency_sha256 = _platform_idempotency(
        config, context.operator_id, action, idempotency_key
    )
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        replay = _action_replay(
            connection, "platform", context.operator_id, action, idempotency_sha256
        )
        if replay is not None:
            _verify_replay(replay, request_sha256)
            row = _license_row(connection, str(replay["result_object_id"]))
            result = _response(connection, row, request_id, trace_id, replay=True)
            connection.commit()
            return result
        row = _license_row(connection, license_id)
        if int(row["revision"]) != payload.expected_revision:
            raise ApiError("edge_license.revision_conflict")
        if str(row["status"]) == "revoked":
            raise ApiError("edge_license.revoked")
        current = _entitlement_row(connection, str(row["current_entitlement_id"]))
        capacity = _capacity(current)
        source = _source_from_entitlement(current)
        now_value = _utcnow()
        generation = int(row["current_generation"]) + 1
        refresh_counter = max(int(row["refresh_counter"]) + 1, generation)
        revocation_generation = int(row["revocation_generation"]) + 1
        signed = _insert_entitlement(
            connection,
            config,
            license_id=license_id,
            workspace_id=str(row["workspace_id"]),
            tenant_id=str(row["tenant_id"]),
            deployment_id=str(row["edge_deployment_id"]),
            device_generation=int(current["device_generation"]),
            source=source,
            capacity=capacity,
            generation=generation,
            refresh_counter=refresh_counter,
            revocation_generation=revocation_generation,
            status="revoked",
            issued_at=now_value,
            expires_at=now_value + timedelta(hours=1),
            grace_ends_at=now_value + timedelta(days=7, hours=1),
        )
        now = _db_time(now_value)
        updated = connection.execute(
            """
            UPDATE edge_licenses SET status='revoked',current_entitlement_id=?,
              current_generation=?,refresh_counter=?,revocation_generation=?,
              revision=revision+1,revoked_by_operator_id=?,revoked_reason_code=?,
              revoked_at=?,updated_at=?
            WHERE edge_license_id=? AND status='active' AND revision=?
            """,
            (
                signed.entitlement.entitlement_id,
                generation,
                refresh_counter,
                revocation_generation,
                context.operator_id,
                payload.reason_code,
                now,
                _changed_stamp(now_value, row["updated_at"]),
                license_id,
                payload.expected_revision,
            ),
        ).rowcount
        if updated != 1:
            raise ApiError("edge_license.revision_conflict")
        _insert_action(
            connection,
            actor_domain="platform",
            actor_id=context.operator_id,
            action=action,
            idempotency_sha256=idempotency_sha256,
            request_sha256=request_sha256,
            result_object_id=license_id,
            now=now,
        )
        _license_event(
            connection,
            workspace_id=str(row["workspace_id"]),
            tenant_id=str(row["tenant_id"]),
            deployment_id=str(row["edge_deployment_id"]),
            license_id=license_id,
            entitlement_id=signed.entitlement.entitlement_id,
            event_type="license.revoked",
            reason_code=payload.reason_code,
            actor_domain="platform",
            actor_id=context.operator_id,
            request_id=request_id,
            trace_id=trace_id,
            now=now,
        )
        _platform_audit(
            connection,
            context,
            action="license.revoked",
            object_id=license_id,
            reason_code=payload.reason_code,
            request_id=request_id,
            trace_id=trace_id,
            after={
                "deployment_id": str(row["edge_deployment_id"]),
                "generation": generation,
                "revocation_generation": revocation_generation,
            },
            now=now,
        )
        current_license = _license_row(connection, license_id)
        result = _response(connection, current_license, request_id, trace_id)
        connection.commit()
        return result
    except ApiError:
        _rollback(connection)
        raise
    except LicenseContractError:
        _rollback(connection)
        raise ApiError("edge_license.integrity_failed") from None
    except sqlite3.IntegrityError:
        _rollback(connection)
        raise ApiError("edge_license.state_conflict") from None
    except (DbError, sqlite3.Error, KeyError, OSError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("edge_license.unavailable") from None
    finally:
        connection.close()


def get_workspace_license(
    config: AppConfig,
    deployment_id: str,
    *,
    session_token: str,
    request_id: str,
    trace_id: str,
) -> LicenseResponse:
    connection = _connect(config)
    try:
        connection.execute("BEGIN")
        context = _workspace_context(
            connection,
            config,
            session_token=session_token,
            csrf_token=None,
            permission="edge.license.read",
        )
        _owned_deployment(connection, context, deployment_id)
        row = connection.execute(
            "SELECT * FROM edge_licenses WHERE edge_deployment_id=?",
            (deployment_id,),
        ).fetchone()
        if row is None or str(row["status"]) == "provisioning":
            raise ApiError("edge_license.not_found")
        result = _response(connection, row, request_id, trace_id)
        connection.rollback()
        return result
    except ApiError:
        _rollback(connection)
        raise
    except (DbError, sqlite3.Error, KeyError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("edge_license.unavailable") from None
    finally:
        connection.close()


def refresh_workspace_license(
    config: AppConfig,
    license_id: str,
    payload: WorkspaceLicenseRefreshRequest,
    *,
    session_token: str,
    csrf_token: str,
    idempotency_key: str,
    request_id: str,
    trace_id: str,
) -> LicenseResponse:
    action = "edge.license.refresh"
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        context = _workspace_context(
            connection,
            config,
            session_token=session_token,
            csrf_token=csrf_token,
            permission="edge.license.refresh",
        )
        request_sha256 = _digest(
            {"license_id": license_id, **payload.model_dump(mode="json")}
        )
        idempotency_sha256 = _workspace_idempotency(
            config, context, action, idempotency_key
        )
        replay = _action_replay(
            connection, "workspace", context.account_id, action, idempotency_sha256
        )
        if replay is not None:
            _verify_replay(replay, request_sha256)
            row = _license_row(connection, str(replay["result_object_id"]))
            result = _response(connection, row, request_id, trace_id, replay=True)
            connection.commit()
            return result
        row = _license_row(connection, license_id)
        if (
            str(row["workspace_id"]) != context.workspace_id
            or str(row["tenant_id"]) != context.tenant_id
        ):
            raise ApiError("edge_license.not_found")
        if str(row["status"]) != "active":
            raise ApiError("edge_license.revoked")
        if int(row["revision"]) != payload.expected_revision:
            raise ApiError("edge_license.revision_conflict")
        now_value = _utcnow()
        source = _commercial_source(
            connection,
            workspace_id=context.workspace_id,
            tenant_id=context.tenant_id,
            requested_order_id=None,
            now=now_value,
        )
        expires_at = now_value + _DEFAULT_REFRESH_LIFETIME
        if source.effective_end is not None:
            expires_at = min(expires_at, source.effective_end)
        _validate_expiry(now_value, expires_at, source.effective_end)
        current = _entitlement_row(connection, str(row["current_entitlement_id"]))
        current_expires = _datetime(current["expires_at"])
        current_grace = _datetime(current["grace_ends_at"]) - current_expires
        capacity = _capacity(current)
        generation = int(row["current_generation"]) + 1
        refresh_counter = max(int(row["refresh_counter"]) + 1, generation)
        refresh_request_id = _id("lrr_")
        now = _db_time(now_value)
        connection.execute(
            """
            INSERT INTO edge_license_refresh_requests(
              refresh_request_id,workspace_id,tenant_id,edge_deployment_id,
              edge_license_id,expected_license_revision,status,reason_code,
              requested_by_account_id,fulfilled_entitlement_id,revision,
              requested_at,completed_at,updated_at)
            VALUES(?,?,?,?,?,?,'requested',?,?,NULL,1,?,NULL,?)
            """,
            (
                refresh_request_id,
                context.workspace_id,
                context.tenant_id,
                row["edge_deployment_id"],
                license_id,
                payload.expected_revision,
                payload.reason_code,
                context.account_id,
                now,
                now,
            ),
        )
        signed = _insert_entitlement(
            connection,
            config,
            license_id=license_id,
            workspace_id=context.workspace_id,
            tenant_id=context.tenant_id,
            deployment_id=str(row["edge_deployment_id"]),
            device_generation=_device_generation(
                connection, str(row["edge_deployment_id"])
            ),
            source=source,
            capacity=capacity,
            generation=generation,
            refresh_counter=refresh_counter,
            revocation_generation=int(row["revocation_generation"]),
            status="active",
            issued_at=now_value,
            expires_at=expires_at,
            grace_ends_at=expires_at + current_grace,
        )
        updated = connection.execute(
            """
            UPDATE edge_licenses SET current_entitlement_id=?,current_generation=?,
              refresh_counter=?,revision=revision+1,updated_at=?
            WHERE edge_license_id=? AND status='active' AND revision=?
            """,
            (
                signed.entitlement.entitlement_id,
                generation,
                refresh_counter,
                _changed_stamp(now_value, row["updated_at"]),
                license_id,
                payload.expected_revision,
            ),
        ).rowcount
        if updated != 1:
            raise ApiError("edge_license.revision_conflict")
        connection.execute(
            """
            UPDATE edge_license_refresh_requests SET status='fulfilled',
              fulfilled_entitlement_id=?,revision=revision+1,completed_at=?,updated_at=?
            WHERE refresh_request_id=? AND status='requested' AND revision=1
            """,
            (
                signed.entitlement.entitlement_id,
                now,
                _changed_stamp(now_value, now),
                refresh_request_id,
            ),
        )
        _insert_action(
            connection,
            actor_domain="workspace",
            actor_id=context.account_id,
            action=action,
            idempotency_sha256=idempotency_sha256,
            request_sha256=request_sha256,
            result_object_id=license_id,
            now=now,
        )
        _license_event(
            connection,
            workspace_id=context.workspace_id,
            tenant_id=context.tenant_id,
            deployment_id=str(row["edge_deployment_id"]),
            license_id=license_id,
            entitlement_id=signed.entitlement.entitlement_id,
            event_type="license.refreshed",
            reason_code=payload.reason_code,
            actor_domain="workspace",
            actor_id=context.account_id,
            request_id=request_id,
            trace_id=trace_id,
            now=now,
        )
        current_license = _license_row(connection, license_id)
        result = _response(connection, current_license, request_id, trace_id)
        connection.commit()
        return result
    except ApiError:
        _rollback(connection)
        raise
    except LicenseContractError:
        _rollback(connection)
        raise ApiError("edge_license.integrity_failed") from None
    except sqlite3.IntegrityError:
        _rollback(connection)
        raise ApiError("edge_license.state_conflict") from None
    except (DbError, sqlite3.Error, KeyError, OSError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("edge_license.unavailable") from None
    finally:
        connection.close()


def pull_agent_license(
    config: AppConfig,
    deployment_id: str,
    *,
    verified_fingerprint: str,
    edge_session_token: str,
    request_id: str,
    trace_id: str,
) -> AgentLicenseResponse | None:
    connection = _connect(config)
    try:
        connection.execute("BEGIN")
        agent = _authenticated_agent(
            connection, config, deployment_id, verified_fingerprint, edge_session_token
        )
        _require_license_capability(agent)
        row = connection.execute(
            "SELECT * FROM edge_licenses WHERE edge_deployment_id=?",
            (deployment_id,),
        ).fetchone()
        if row is None or row["current_entitlement_id"] is None:
            connection.rollback()
            return None
        entitlement = _entitlement_row(connection, str(row["current_entitlement_id"]))
        signed = SignedEntitlement.model_validate_json(str(entitlement["envelope_json"]))
        result = AgentLicenseResponse(
            deployment_id=deployment_id,
            entitlement=signed,
            server_time=_utcnow(),
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
        raise ApiError("edge_license.unavailable") from None
    finally:
        connection.close()


def acknowledge_agent_license(
    config: AppConfig,
    deployment_id: str,
    payload: AgentLicenseAcknowledgementRequest,
    *,
    verified_fingerprint: str,
    edge_session_token: str,
    request_id: str,
    trace_id: str,
) -> AgentLicenseAcknowledgementResponse:
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        agent = _authenticated_agent(
            connection, config, deployment_id, verified_fingerprint, edge_session_token
        )
        _require_license_capability(agent)
        entitlement = connection.execute(
            """
            SELECT * FROM edge_signed_entitlements
            WHERE edge_deployment_id=? AND entitlement_id=? AND generation=?
            """,
            (deployment_id, payload.entitlement_id, payload.generation),
        ).fetchone()
        if entitlement is None:
            raise ApiError("edge_license.ack_invalid")
        now_value = _utcnow()
        trusted_wall_time = _aware(payload.trusted_wall_time)
        bucket_started = _aware(payload.bucket_started_at)
        bucket_ended = _aware(payload.bucket_ended_at)
        if (
            trusted_wall_time > now_value + _CLOCK_SKEW
            or trusted_wall_time < now_value - timedelta(days=30)
            or bucket_ended > now_value + _CLOCK_SKEW
            or bucket_ended - bucket_started > timedelta(days=1)
        ):
            raise ApiError("edge_license.ack_invalid")
        nonce_sha256 = hashlib.sha256(
            (
                f"prd128\0{deployment_id}\0{payload.entitlement_id}\0"
                f"{payload.generation}\0{payload.attempt_nonce}"
            ).encode("ascii")
        ).hexdigest()
        existing = connection.execute(
            """
            SELECT * FROM edge_license_acknowledgements
            WHERE edge_deployment_id=? AND generation=?
            """,
            (deployment_id, payload.generation),
        ).fetchone()
        if existing is not None:
            _verify_ack_replay(existing, payload, nonce_sha256)
            usage = connection.execute(
                """
                SELECT * FROM edge_license_lease_reports
                WHERE edge_deployment_id=? AND bucket_started_at=?
                """,
                (deployment_id, _db_time(bucket_started)),
            ).fetchone()
            if usage is None:
                _insert_usage_report(
                    connection,
                    entitlement=entitlement,
                    deployment_id=deployment_id,
                    payload=payload,
                    bucket_started=bucket_started,
                    bucket_ended=bucket_ended,
                    now=_db_time(now_value),
                )
            else:
                _verify_usage_replay(usage, payload, bucket_started, bucket_ended)
            result = _ack_response(existing, deployment_id, request_id, trace_id, replay=True)
            connection.commit()
            return result
        now = _db_time(now_value)
        acknowledgement_id = _id("lak_")
        connection.execute(
            """
            INSERT INTO edge_license_acknowledgements(
              license_ack_id,workspace_id,tenant_id,edge_deployment_id,entitlement_id,
              generation,disposition,decision_status,reason_code,trusted_wall_time,
              attempt_nonce_sha256,request_id,trace_id,acknowledged_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                acknowledgement_id,
                entitlement["workspace_id"],
                entitlement["tenant_id"],
                deployment_id,
                payload.entitlement_id,
                payload.generation,
                payload.disposition,
                payload.decision_status,
                payload.reason_code,
                _db_time(trusted_wall_time),
                nonce_sha256,
                _identifier(request_id),
                _identifier(trace_id),
                now,
            ),
        )
        _insert_usage_report(
            connection,
            entitlement=entitlement,
            deployment_id=deployment_id,
            payload=payload,
            bucket_started=bucket_started,
            bucket_ended=bucket_ended,
            now=now,
        )
        request_sha256 = _digest(
            {
                **payload.model_dump(mode="json", exclude={"attempt_nonce"}),
                "attempt_nonce_sha256": nonce_sha256,
            }
        )
        _insert_action(
            connection,
            actor_domain="agent",
            actor_id=str(agent["edge_session_id"]),
            action="edge.license.ack",
            idempotency_sha256=nonce_sha256,
            request_sha256=request_sha256,
            result_object_id=acknowledgement_id,
            now=now,
        )
        if payload.denied_count > 0 or payload.reason_code == "edge_license.capacity_exceeded":
            license_row = connection.execute(
                "SELECT * FROM edge_licenses WHERE edge_deployment_id=?",
                (deployment_id,),
            ).fetchone()
            if license_row is not None:
                _license_event(
                    connection,
                    workspace_id=str(entitlement["workspace_id"]),
                    tenant_id=str(entitlement["tenant_id"]),
                    deployment_id=deployment_id,
                    license_id=str(license_row["edge_license_id"]),
                    entitlement_id=payload.entitlement_id,
                    event_type="license.capacity_exceeded",
                    reason_code="edge_license.capacity_exceeded",
                    actor_domain="agent",
                    actor_id=str(agent["edge_session_id"]),
                    request_id=request_id,
                    trace_id=trace_id,
                    now=now,
                )
        ack = connection.execute(
            "SELECT * FROM edge_license_acknowledgements WHERE license_ack_id=?",
            (acknowledgement_id,),
        ).fetchone()
        result = _ack_response(ack, deployment_id, request_id, trace_id)
        connection.commit()
        return result
    except ApiError:
        _rollback(connection)
        raise
    except sqlite3.IntegrityError:
        _rollback(connection)
        raise ApiError("edge_license.ack_conflict") from None
    except (DbError, sqlite3.Error, KeyError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("edge_license.unavailable") from None
    finally:
        connection.close()


def _insert_entitlement(
    connection: sqlite3.Connection,
    config: AppConfig,
    *,
    license_id: str,
    workspace_id: str,
    tenant_id: str,
    deployment_id: str,
    device_generation: int,
    source: _CommercialSource,
    capacity: LicenseCapacity,
    generation: int,
    refresh_counter: int,
    revocation_generation: int,
    status: str,
    issued_at: datetime,
    expires_at: datetime,
    grace_ends_at: datetime,
) -> SignedEntitlement:
    entitlement = LicenseEntitlement(
        entitlement_id=_id("let_"),
        license_id=license_id,
        workspace_id=workspace_id,
        tenant_id=tenant_id,
        deployment_id=deployment_id,
        device_generation=device_generation,
        commercial_source_type=source.source_type,
        commercial_source_id=source.source_id,
        commercial_source_revision=source.snapshot_version,
        generation=generation,
        refresh_counter=refresh_counter,
        revocation_generation=revocation_generation,
        status=status,
        capacity=capacity,
        issued_at=issued_at,
        not_before=issued_at,
        expires_at=expires_at,
        grace_ends_at=grace_ends_at,
    )
    private_key, key_id = _signing_key(config)
    signed = sign_license_entitlement(
        entitlement, private_key=private_key, key_id=key_id
    )
    envelope = canonical_edge_json_bytes(
        signed.model_dump(mode="json"), maximum=32_768
    )
    connection.execute(
        """
        INSERT INTO edge_signed_entitlements(
          entitlement_id,workspace_id,tenant_id,edge_deployment_id,edge_license_id,
          source_entitlement_snapshot_id,source_order_id,commercial_source_revision,
          device_generation,generation,refresh_counter,revocation_generation,status,
          node_limit,concurrent_request_limit,feature_codes_json,envelope_json,
          manifest_sha256,envelope_sha256,signature_key_id,signing_public_key_b64,
          signature_value,signature_purpose,issued_at,not_before,expires_at,
          grace_ends_at,created_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            entitlement.entitlement_id,
            workspace_id,
            tenant_id,
            deployment_id,
            license_id,
            source.snapshot_id,
            source.order_id,
            source.snapshot_version,
            device_generation,
            generation,
            refresh_counter,
            revocation_generation,
            status,
            capacity.node_limit,
            capacity.concurrent_request_limit,
            json.dumps(capacity.features, ensure_ascii=True, separators=(",", ":")),
            envelope.decode("utf-8"),
            signed.manifest_sha256,
            hashlib.sha256(envelope).hexdigest(),
            signed.signature.key_id,
            signed.signature.public_key,
            signed.signature.value,
            signed.signature_purpose,
            _db_time(issued_at),
            _db_time(issued_at),
            _db_time(expires_at),
            _db_time(grace_ends_at),
            _db_time(issued_at),
        ),
    )
    grants = (
        ("node", capacity.node_limit, None),
        ("concurrent_request", capacity.concurrent_request_limit, None),
        *(("feature", None, feature) for feature in capacity.features),
    )
    for dimension, integer_limit, feature_code in grants:
        connection.execute(
            """
            INSERT INTO edge_capacity_grants(
              capacity_grant_id,workspace_id,tenant_id,edge_deployment_id,
              entitlement_id,dimension,integer_limit,feature_code,generation,created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            (
                _id("cpg_"),
                workspace_id,
                tenant_id,
                deployment_id,
                entitlement.entitlement_id,
                dimension,
                integer_limit,
                feature_code,
                generation,
                _db_time(issued_at),
            ),
        )
    return signed


def _commercial_source(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    tenant_id: str,
    requested_order_id: str | None,
    now: datetime,
) -> _CommercialSource:
    now_text = _db_time(now)
    row = connection.execute(
        """
        SELECT subscription.subscription_id,subscription.status subscription_status,
          subscription.current_period_ends_at,
          snapshot.entitlement_snapshot_id,snapshot.snapshot_version,
          snapshot.source_order_record_id,snapshot.effective_until,
          source.status source_record_status
        FROM workspace_subscriptions subscription
        JOIN entitlement_snapshots snapshot
          ON snapshot.subscription_id=subscription.subscription_id
         AND snapshot.workspace_id=subscription.workspace_id
         AND snapshot.tenant_id=subscription.tenant_id
        JOIN subscription_order_records source
          ON source.order_record_id=snapshot.source_order_record_id
        WHERE subscription.workspace_id=? AND subscription.tenant_id=?
          AND subscription.status IN('active','trialing')
          AND snapshot.effective_from<=?
          AND (snapshot.effective_until IS NULL OR snapshot.effective_until>?)
        ORDER BY snapshot.snapshot_version DESC LIMIT 1
        """,
        (workspace_id, tenant_id, now_text, now_text),
    ).fetchone()
    if row is None:
        raise ApiError("edge_license.commercial_ineligible")
    ends: list[datetime] = []
    if row["current_period_ends_at"] is not None:
        ends.append(_datetime(row["current_period_ends_at"]))
    if row["effective_until"] is not None:
        ends.append(_datetime(row["effective_until"]))
    if str(row["subscription_status"]) == "trialing":
        trial = connection.execute(
            """
            SELECT ends_at FROM subscription_trials
            WHERE subscription_id=? AND status='active' AND starts_at<=? AND ends_at>?
            ORDER BY cycle DESC LIMIT 1
            """,
            (row["subscription_id"], now_text, now_text),
        ).fetchone()
        if trial is None:
            raise ApiError("edge_license.commercial_ineligible")
        ends.append(_datetime(trial["ends_at"]))
    effective_end = min(ends) if ends else None
    if effective_end is not None and effective_end <= now:
        raise ApiError("edge_license.commercial_ineligible")

    if requested_order_id is not None:
        order = connection.execute(
            """
            SELECT * FROM subscription_orders
            WHERE order_id=? AND workspace_id=? AND tenant_id=?
              AND source_order_record_id=?
            """,
            (
                requested_order_id,
                workspace_id,
                tenant_id,
                row["source_order_record_id"],
            ),
        ).fetchone()
        if order is None:
            raise ApiError("edge_license.commercial_ineligible")
    else:
        order = connection.execute(
            """
            SELECT * FROM subscription_orders
            WHERE workspace_id=? AND tenant_id=? AND source_order_record_id=?
            ORDER BY created_at DESC,order_id DESC LIMIT 1
            """,
            (workspace_id, tenant_id, row["source_order_record_id"]),
        ).fetchone()
    if order is not None and str(order["status"]) not in {"paid", "fulfilled"}:
        raise ApiError("edge_license.commercial_ineligible")
    if order is None and str(row["source_record_status"]) != "not_required":
        raise ApiError("edge_license.commercial_ineligible")
    order_id = str(order["order_id"]) if order is not None else None
    return _CommercialSource(
        snapshot_id=str(row["entitlement_snapshot_id"]),
        snapshot_version=int(row["snapshot_version"]),
        order_id=order_id,
        source_type="subscription_order" if order_id is not None else "entitlement_snapshot",
        source_id=order_id or str(row["entitlement_snapshot_id"]),
        effective_end=effective_end,
    )


def _source_from_entitlement(row: sqlite3.Row) -> _CommercialSource:
    order_id = str(row["source_order_id"]) if row["source_order_id"] is not None else None
    source_type = "subscription_order" if order_id is not None else "entitlement_snapshot"
    return _CommercialSource(
        snapshot_id=str(row["source_entitlement_snapshot_id"]),
        snapshot_version=int(row["commercial_source_revision"]),
        order_id=order_id,
        source_type=source_type,
        source_id=order_id or str(row["source_entitlement_snapshot_id"]),
        effective_end=None,
    )


def _summary(connection: sqlite3.Connection, row: sqlite3.Row) -> LicenseSummary:
    entitlement = _entitlement_row(connection, str(row["current_entitlement_id"]))
    ack = connection.execute(
        """
        SELECT * FROM edge_license_acknowledgements
        WHERE edge_deployment_id=? ORDER BY generation DESC LIMIT 1
        """,
        (row["edge_deployment_id"],),
    ).fetchone()
    usage = connection.execute(
        """
        SELECT * FROM edge_license_lease_reports
        WHERE edge_deployment_id=? ORDER BY bucket_ended_at DESC,lease_report_id DESC LIMIT 1
        """,
        (row["edge_deployment_id"],),
    ).fetchone()
    return LicenseSummary(
        license_id=str(row["edge_license_id"]),
        workspace_id=str(row["workspace_id"]),
        tenant_id=str(row["tenant_id"]),
        deployment_id=str(row["edge_deployment_id"]),
        status=str(row["status"]),
        current_entitlement_id=str(row["current_entitlement_id"]),
        capacity=_capacity(entitlement),
        generation=int(row["current_generation"]),
        refresh_counter=int(row["refresh_counter"]),
        revocation_generation=int(row["revocation_generation"]),
        manifest_sha256=str(entitlement["manifest_sha256"]),
        signing_key_id=str(entitlement["signature_key_id"]),
        issued_at=_datetime(entitlement["issued_at"]),
        not_before=_datetime(entitlement["not_before"]),
        expires_at=_datetime(entitlement["expires_at"]),
        grace_ends_at=_datetime(entitlement["grace_ends_at"]),
        last_acknowledgement=_ack_summary(ack) if ack is not None else None,
        latest_usage=(
            LicenseUsageSummary(
                active_node_units=int(usage["active_node_units"]),
                active_request_units=int(usage["active_request_units"]),
                denied_count=int(usage["denied_count"]),
                bucket_started_at=_datetime(usage["bucket_started_at"]),
                bucket_ended_at=_datetime(usage["bucket_ended_at"]),
            )
            if usage is not None
            else None
        ),
        revision=int(row["revision"]),
        updated_at=_datetime(row["updated_at"]),
        revoked_reason_code=(
            str(row["revoked_reason_code"])
            if row["revoked_reason_code"] is not None
            else None
        ),
        revoked_at=(
            _datetime(row["revoked_at"]) if row["revoked_at"] is not None else None
        ),
    )


def _response(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    request_id: str,
    trace_id: str,
    *,
    replay: bool = False,
) -> LicenseResponse:
    return LicenseResponse(
        license=_summary(connection, row),
        idempotent_replay=replay,
        request_id=_identifier(request_id),
        trace_id=_identifier(trace_id),
    )


def _ack_response(
    row: sqlite3.Row,
    deployment_id: str,
    request_id: str,
    trace_id: str,
    *,
    replay: bool = False,
) -> AgentLicenseAcknowledgementResponse:
    return AgentLicenseAcknowledgementResponse(
        deployment_id=deployment_id,
        acknowledgement=_ack_summary(row),
        idempotent_replay=replay,
        request_id=_identifier(request_id),
        trace_id=_identifier(trace_id),
    )


def _ack_summary(row: sqlite3.Row) -> LicenseAcknowledgementSummary:
    return LicenseAcknowledgementSummary(
        acknowledgement_id=str(row["license_ack_id"]),
        generation=int(row["generation"]),
        disposition=str(row["disposition"]),
        decision_status=str(row["decision_status"]),
        reason_code=str(row["reason_code"]),
        acknowledged_at=_datetime(row["acknowledged_at"]),
    )


def _capacity(row: sqlite3.Row) -> LicenseCapacity:
    features = json.loads(str(row["feature_codes_json"]))
    if type(features) is not list:
        raise ValueError("invalid license features")
    return LicenseCapacity(
        node_limit=int(row["node_limit"]),
        concurrent_request_limit=int(row["concurrent_request_limit"]),
        features=features,
    )


def _license_row(connection: sqlite3.Connection, license_id: str) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM edge_licenses WHERE edge_license_id=?", (license_id,)
    ).fetchone()
    if row is None or row["current_entitlement_id"] is None:
        raise ApiError("edge_license.not_found")
    return row


def _entitlement_row(connection: sqlite3.Connection, entitlement_id: str) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM edge_signed_entitlements WHERE entitlement_id=?",
        (entitlement_id,),
    ).fetchone()
    if row is None:
        raise ApiError("edge_license.integrity_failed")
    return row


def _device_generation(connection: sqlite3.Connection, deployment_id: str) -> int:
    row = connection.execute(
        """
        SELECT generation FROM edge_device_identities
        WHERE edge_deployment_id=? AND status='active'
        ORDER BY generation DESC LIMIT 1
        """,
        (deployment_id,),
    ).fetchone()
    if row is None:
        raise ApiError("edge_license.device_ineligible")
    return int(row["generation"])


def _signing_key(config: AppConfig) -> tuple[Ed25519PrivateKey, str]:
    seed = hmac.new(
        config.platform_session_secret.encode("utf-8"),
        b"metahub/prd128/license-entitlement-signing-key/v1",
        hashlib.sha256,
    ).digest()
    private_key = Ed25519PrivateKey.from_private_bytes(seed)
    public = private_key.public_key().public_bytes_raw()
    return private_key, f"edge-license-{hashlib.sha256(public).hexdigest()[:16]}"


def _action_replay(
    connection: sqlite3.Connection,
    actor_domain: str,
    actor_id: str,
    action: str,
    idempotency_sha256: str,
) -> sqlite3.Row | None:
    return connection.execute(
        """
        SELECT * FROM edge_license_action_commands
        WHERE actor_domain=? AND actor_id=? AND action_code=? AND idempotency_sha256=?
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
        INSERT INTO edge_license_action_commands(
          license_action_command_id,actor_domain,actor_id,action_code,
          idempotency_sha256,request_sha256,result_object_id,created_at)
        VALUES(?,?,?,?,?,?,?,?)
        """,
        (
            _id("lac_"),
            actor_domain,
            actor_id,
            action,
            idempotency_sha256,
            request_sha256,
            result_object_id,
            now,
        ),
    )


def _license_event(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    tenant_id: str,
    deployment_id: str,
    license_id: str,
    entitlement_id: str,
    event_type: str,
    reason_code: str,
    actor_domain: str,
    actor_id: str,
    request_id: str,
    trace_id: str,
    now: str,
) -> None:
    audit_id = _id("lau_")
    connection.execute(
        """
        INSERT INTO edge_license_audits(
          license_audit_id,workspace_id,tenant_id,edge_deployment_id,
          edge_license_id,entitlement_id,event_type,reason_code,actor_domain,
          actor_id,request_id,trace_id,occurred_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            audit_id,
            workspace_id,
            tenant_id,
            deployment_id,
            license_id,
            entitlement_id,
            event_type,
            reason_code,
            actor_domain,
            actor_id,
            _identifier(request_id),
            _identifier(trace_id),
            now,
        ),
    )
    for delivery_domain in ("workspace", "billing", "security"):
        connection.execute(
            """
            INSERT INTO edge_license_outbox(
              license_outbox_id,workspace_id,tenant_id,license_audit_id,
              delivery_domain,status,attempt_count,available_at,delivered_at,
              created_at,updated_at)
            VALUES(?,?,?,?,?,'pending',0,?,NULL,?,?)
            """,
            (
                _id("lou_"),
                workspace_id,
                tenant_id,
                audit_id,
                delivery_domain,
                now,
                now,
                now,
            ),
        )


def _platform_audit(
    connection: sqlite3.Connection,
    context: PlatformContext,
    *,
    action: str,
    object_id: str,
    reason_code: str,
    request_id: str,
    trace_id: str,
    after: object,
    now: str,
) -> None:
    connection.execute(
        """
        INSERT INTO platform_audit_logs(
          audit_id,actor_domain,actor_id,action_code,object_type,object_id,
          reason_code,request_id,trace_id,before_sha256,after_sha256,occurred_at)
        VALUES(?,'platform',?,?,'edge_license',?,?,?,?,NULL,?,?)
        """,
        (
            _id("pau_"),
            context.operator_id,
            action,
            object_id,
            reason_code,
            _identifier(request_id),
            _identifier(trace_id),
            _digest(after),
            now,
        ),
    )


def _platform_idempotency(
    config: AppConfig, operator_id: str, action: str, value: str
) -> str:
    if type(value) is not str or _IDEMPOTENCY_RE.fullmatch(value) is None:
        raise ApiError("edge_license.request_invalid")
    return hmac.new(
        config.platform_session_secret.encode("utf-8"),
        f"metahub/prd128/platform-idempotency/v1\0{operator_id}\0{action}\0{value}".encode(
            "ascii"
        ),
        hashlib.sha256,
    ).hexdigest()


def _require_platform(context: PlatformContext, permission: str) -> None:
    if permission not in context.permissions:
        raise ApiError("platform.permission_denied")


def _require_license_capability(agent: sqlite3.Row) -> None:
    try:
        capabilities = json.loads(str(agent["capability_codes_json"]))
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        raise ApiError("edge.authentication_failed") from None
    if type(capabilities) is not list or "license" not in capabilities:
        raise ApiError("edge.authentication_failed")


def _verify_replay(row: sqlite3.Row, request_sha256: str) -> None:
    if not hmac.compare_digest(str(row["request_sha256"]), request_sha256):
        raise ApiError("edge_license.idempotency_conflict")


def _verify_ack_replay(
    row: sqlite3.Row,
    payload: AgentLicenseAcknowledgementRequest,
    nonce_sha256: str,
) -> None:
    if not (
        hmac.compare_digest(str(row["entitlement_id"]), payload.entitlement_id)
        and str(row["disposition"]) == payload.disposition
        and str(row["decision_status"]) == payload.decision_status
        and str(row["reason_code"]) == payload.reason_code
        and hmac.compare_digest(str(row["attempt_nonce_sha256"]), nonce_sha256)
    ):
        raise ApiError("edge_license.ack_conflict")


def _verify_usage_replay(
    row: sqlite3.Row,
    payload: AgentLicenseAcknowledgementRequest,
    bucket_started: datetime,
    bucket_ended: datetime,
) -> None:
    if not (
        int(row["active_node_units"]) == payload.active_node_units
        and int(row["active_request_units"]) == payload.active_request_units
        and int(row["denied_count"]) == payload.denied_count
        and str(row["bucket_started_at"]) == _db_time(bucket_started)
        and str(row["bucket_ended_at"]) == _db_time(bucket_ended)
    ):
        raise ApiError("edge_license.ack_conflict")


def _insert_usage_report(
    connection: sqlite3.Connection,
    *,
    entitlement: sqlite3.Row,
    deployment_id: str,
    payload: AgentLicenseAcknowledgementRequest,
    bucket_started: datetime,
    bucket_ended: datetime,
    now: str,
) -> None:
    connection.execute(
        """
        INSERT INTO edge_license_lease_reports(
          lease_report_id,workspace_id,tenant_id,edge_deployment_id,
          entitlement_id,generation,active_node_units,active_request_units,
          denied_count,bucket_started_at,bucket_ended_at,created_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            _id("lrp_"),
            entitlement["workspace_id"],
            entitlement["tenant_id"],
            deployment_id,
            payload.entitlement_id,
            payload.generation,
            payload.active_node_units,
            payload.active_request_units,
            payload.denied_count,
            _db_time(bucket_started),
            _db_time(bucket_ended),
            now,
        ),
    )


def _validate_expiry(
    now: datetime, expires_at: datetime, commercial_end: datetime | None
) -> None:
    if (
        expires_at < now + _MIN_LICENSE_LIFETIME
        or expires_at > now + _MAX_LICENSE_LIFETIME
        or (commercial_end is not None and expires_at > commercial_end)
    ):
        raise ApiError("edge_license.expiry_invalid")


def _changed_stamp(now: datetime, previous: object) -> str:
    prior = _datetime(previous)
    candidate = _db_time(now)
    if candidate > str(previous):
        return candidate
    return _db_time(prior + timedelta(milliseconds=1))


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ApiError("edge_license.request_invalid")
    return value.astimezone(UTC)


__all__ = (
    "acknowledge_agent_license",
    "get_workspace_license",
    "issue_platform_license",
    "list_platform_licenses",
    "pull_agent_license",
    "refresh_workspace_license",
    "revoke_platform_license",
)
