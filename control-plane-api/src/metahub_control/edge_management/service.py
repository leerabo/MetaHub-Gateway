"""Transactional registration, certificate, session, and revocation for PRD-120."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import sqlite3
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from metahub_shared.config_distribution import canonical_json_bytes
from metahub_shared.db import connect_database
from metahub_shared.db.errors import DbError
from metahub_shared.private_gateway import (
    EdgeCsr,
    certificate_sha256,
    parse_edge_csr,
    validate_edge_challenge,
    validate_edge_deployment_id,
    validate_edge_nonce,
    validate_edge_registration_token,
    verify_registration_challenge_proof,
)
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError

from metahub_control.platform_access.service import PlatformContext
from metahub_control.workspace_access.service import (
    WorkspaceSessionContext,
    authenticate_workspace_session_in_transaction,
)

from .models import (
    EdgeAgentRegistrationRequest,
    EdgeCertificateResponse,
    EdgeCertificateRotationRequest,
    EdgeCertificateSummary,
    EdgeDeploymentListResponse,
    EdgeDeploymentRevokeRequest,
    EdgeDeploymentRevokeResponse,
    EdgeDeploymentSummary,
    EdgeDeviceIdentitySummary,
    EdgeEventSummary,
    EdgeSessionCreateRequest,
    EdgeSessionResponse,
    PlatformEdgeFleetResponse,
    PlatformEdgeFleetSummary,
    WorkspaceRegistrationCreateRequest,
    WorkspaceRegistrationResponse,
)

_REGION_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,38}[a-z0-9])?$")
_AGENT_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")
_HEX_64_RE = re.compile(r"^[0-9a-f]{64}$")
_CAPABILITY_RE = re.compile(r"^[a-z][a-z0-9_]{1,47}$")
_ALLOWED_CAPABILITIES = frozenset(
    {
        "backup",
        "config_apply",
        "diagnostics",
        "isolated_mode",
        "license",
        "secret_reference",
        "telemetry",
        "upgrade",
    }
)
_AGENT_PERMISSIONS = (
    "edge.self.connect",
    "edge.self.register",
    "edge.self.rotate",
)
_CA_KEY_ID = "edge-device-ca-v1"
_CERTIFICATE_TTL = timedelta(days=7)
_ROTATION_DRAIN = timedelta(minutes=5)


def create_workspace_registration(
    config: AppConfig,
    *,
    session_token: str,
    csrf_token: str,
    idempotency_key: str,
    payload: WorkspaceRegistrationCreateRequest,
    request_id: str,
    trace_id: str,
) -> WorkspaceRegistrationResponse:
    """Create a tenant-owned deployment and return its credential exactly once."""

    region_code = _region(payload.region_code)
    request_sha256 = _digest(payload.model_dump(mode="json"))
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
        idempotency_sha256 = _workspace_idempotency(
            config, context, "edge.registration.create", idempotency_key
        )
        replay = _workspace_command(
            connection, context, "edge.registration.create", idempotency_sha256
        )
        if replay is not None:
            if str(replay["request_sha256"]) != request_sha256:
                raise ApiError("edge.idempotency_conflict")
            intent = connection.execute(
                """
                SELECT * FROM edge_registration_intents
                WHERE workspace_id=? AND tenant_id=? AND created_by_account_id=?
                  AND idempotency_sha256=?
                """,
                (
                    context.workspace_id,
                    context.tenant_id,
                    context.account_id,
                    idempotency_sha256,
                ),
            ).fetchone()
            deployment = _owned_deployment(connection, context, str(replay["result_object_id"]))
            if intent is None:
                raise ApiError("edge.unavailable")
            result = WorkspaceRegistrationResponse(
                deployment=_deployment_summary(connection, deployment),
                registration_intent_id=str(intent["edge_registration_intent_id"]),
                expires_at=_datetime(intent["expires_at"]),
                idempotent_replay=True,
                request_id=_identifier(request_id),
                trace_id=_identifier(trace_id),
            )
            connection.commit()
            return result

        now = _utcnow()
        now_db = _db_time(now)
        expires_at = now + timedelta(seconds=payload.token_ttl_seconds)
        deployment_id = _id("edp_")
        intent_id = _id("eri_")
        registration_token = "erg_" + secrets.token_urlsafe(32)
        challenge = "ech_" + secrets.token_urlsafe(32)
        connection.execute(
            """
            INSERT INTO edge_deployments(
              edge_deployment_id,workspace_id,tenant_id,display_name,region_code,mode,status,
              revision,revocation_generation,agent_version,health_code,last_connected_at,
              certificate_expires_at,created_by_account_id,revoked_by_account_id,
              revoked_reason_code,created_at,updated_at,revoked_at)
            VALUES(?,?,?,?,?,?,'pending',1,1,NULL,'not_registered',NULL,NULL,?,NULL,NULL,?,?,NULL)
            """,
            (
                deployment_id,
                context.workspace_id,
                context.tenant_id,
                payload.display_name,
                region_code,
                payload.mode,
                context.account_id,
                now_db,
                now_db,
            ),
        )
        _insert_default_telemetry_policy_if_available(
            connection,
            context,
            deployment_id=deployment_id,
            now=now_db,
        )
        connection.execute(
            """
            INSERT INTO edge_registration_intents(
              edge_registration_intent_id,workspace_id,tenant_id,edge_deployment_id,
              created_by_account_id,token_hmac,token_key_version,challenge_hmac,
              idempotency_sha256,request_sha256,csr_sha256,public_key_sha256,
              consumed_request_sha256,result_identity_id,result_certificate_id,status,
              expires_at,consumed_at,created_at,updated_at)
            VALUES(?,?,?,?,?,?,'edge-registration-v1',?,?,?,NULL,NULL,NULL,NULL,NULL,'issued',
              ?,NULL,?,?)
            """,
            (
                intent_id,
                context.workspace_id,
                context.tenant_id,
                deployment_id,
                context.account_id,
                _secret_hmac(config, "edge-registration-token-v1", registration_token),
                _secret_hmac(config, "edge-registration-challenge-v1", challenge),
                idempotency_sha256,
                request_sha256,
                _db_time(expires_at),
                now_db,
                now_db,
            ),
        )
        _insert_workspace_command(
            connection,
            context,
            action="edge.registration.create",
            idempotency_sha256=idempotency_sha256,
            request_sha256=request_sha256,
            result_object_id=deployment_id,
            now=now_db,
        )
        _refresh_health_if_available(
            connection,
            deployment_id,
            request_id=request_id,
            trace_id=trace_id,
            now=now,
        )
        deployment = _owned_deployment(connection, context, deployment_id)
        result = WorkspaceRegistrationResponse(
            deployment=_deployment_summary(connection, deployment),
            registration_intent_id=intent_id,
            registration_token=registration_token,
            challenge=challenge,
            expires_at=expires_at,
            idempotent_replay=False,
            request_id=_identifier(request_id),
            trace_id=_identifier(trace_id),
        )
        connection.commit()
        return result
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, DbError, KeyError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("edge.unavailable") from None
    finally:
        connection.close()


def list_workspace_deployments(
    config: AppConfig,
    *,
    session_token: str,
    request_id: str,
    trace_id: str,
) -> EdgeDeploymentListResponse:
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
        rows = connection.execute(
            """
            SELECT * FROM edge_deployments
            WHERE workspace_id=? AND tenant_id=?
            ORDER BY created_at DESC,edge_deployment_id DESC
            """,
            (context.workspace_id, context.tenant_id),
        ).fetchall()
        result = EdgeDeploymentListResponse(
            deployments=tuple(_deployment_summary(connection, row) for row in rows),
            request_id=_identifier(request_id),
            trace_id=_identifier(trace_id),
        )
        connection.commit()
        return result
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, DbError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        _rollback(connection)
        raise ApiError("edge.unavailable") from None
    finally:
        connection.close()


def register_edge_agent(
    config: AppConfig,
    payload: EdgeAgentRegistrationRequest,
    *,
    request_id: str,
    trace_id: str,
) -> EdgeCertificateResponse:
    """Consume one registration credential and atomically create the first identity."""

    try:
        token = validate_edge_registration_token(payload.registration_token)
        deployment_id = validate_edge_deployment_id(payload.deployment_id)
        challenge = validate_edge_challenge(payload.challenge)
    except ValueError:
        raise ApiError("edge.authentication_failed") from None
    token_hmac = _secret_hmac(config, "edge-registration-token-v1", token)
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            """
            SELECT intent.*,deployment.status deployment_status,
              deployment.revocation_generation,deployment.revision deployment_revision
            FROM edge_registration_intents intent
            JOIN edge_deployments deployment
              ON deployment.workspace_id=intent.workspace_id
             AND deployment.tenant_id=intent.tenant_id
             AND deployment.edge_deployment_id=intent.edge_deployment_id
            WHERE intent.token_hmac=?
            """,
            (token_hmac,),
        ).fetchone()
        if row is None or not hmac.compare_digest(str(row["edge_deployment_id"]), deployment_id):
            raise ApiError("edge.authentication_failed")
        if not hmac.compare_digest(
            str(row["challenge_hmac"]),
            _secret_hmac(config, "edge-registration-challenge-v1", challenge),
        ):
            _reject_known_agent(
                connection,
                row,
                reason="credential_invalid",
                request_id=request_id,
                trace_id=trace_id,
            )
        now = _utcnow()
        try:
            parsed_csr = parse_edge_csr(payload.csr_pem)
        except ValueError:
            raise ApiError("edge.csr_invalid") from None
        if not verify_registration_challenge_proof(
            payload.challenge_proof,
            token,
            deployment_id=deployment_id,
            challenge=challenge,
            csr_sha256=parsed_csr.csr_sha256,
        ):
            _reject_known_agent(
                connection,
                row,
                reason="credential_invalid",
                request_id=request_id,
                trace_id=trace_id,
            )
        capabilities = _capabilities(payload.capability_codes)
        _agent_version(payload.agent_version)
        hardware_digest = _optional_digest(payload.hardware_proof_sha256)
        consumed_request_sha256 = _digest(
            payload.model_dump(mode="json", exclude={"registration_token"})
        )
        if str(row["status"]) == "consumed":
            if not hmac.compare_digest(
                str(row["consumed_request_sha256"]), consumed_request_sha256
            ):
                _reject_known_agent(
                    connection,
                    row,
                    reason="credential_replayed",
                    request_id=request_id,
                    trace_id=trace_id,
                )
            result = _certificate_response(
                connection,
                config,
                deployment_id=deployment_id,
                identity_id=str(row["result_identity_id"]),
                certificate_id=str(row["result_certificate_id"]),
                idempotent_replay=True,
                request_id=request_id,
                trace_id=trace_id,
            )
            connection.commit()
            return result
        if str(row["status"]) in {"expired", "revoked"} or _datetime(row["expires_at"]) <= now:
            if str(row["status"]) == "issued":
                connection.execute(
                    "UPDATE edge_registration_intents SET status='expired',updated_at=? "
                    "WHERE edge_registration_intent_id=? AND status='issued'",
                    (_db_time(now), row["edge_registration_intent_id"]),
                )
            _reject_known_agent(
                connection,
                row,
                reason="credential_expired",
                request_id=request_id,
                trace_id=trace_id,
            )
        if str(row["status"]) != "issued" or str(row["deployment_status"]) != "pending":
            _reject_known_agent(
                connection,
                row,
                reason="credential_invalid",
                request_id=request_id,
                trace_id=trace_id,
            )
        if (
            connection.execute(
                "SELECT 1 FROM edge_device_identities WHERE public_key_sha256=?",
                (parsed_csr.public_key_sha256,),
            ).fetchone()
            is not None
        ):
            _reject_known_agent(
                connection,
                row,
                reason="credential_replayed",
                request_id=request_id,
                trace_id=trace_id,
            )

        identity_id = _id("edi_")
        certificate_id = _id("edc_")
        issued = _issue_certificate(
            config,
            parsed_csr,
            deployment_id=deployment_id,
            identity_id=identity_id,
            generation=1,
            now=now,
        )
        now_db = _db_time(now)
        connection.execute(
            """
            INSERT INTO edge_device_identities(
              edge_device_identity_id,workspace_id,tenant_id,edge_deployment_id,
              public_key_sha256,hardware_proof_sha256,capability_codes_json,agent_version,
              generation,status,created_at,updated_at,revoked_at)
            VALUES(?,?,?,?,?,?,?,?,1,'active',?,?,NULL)
            """,
            (
                identity_id,
                row["workspace_id"],
                row["tenant_id"],
                deployment_id,
                parsed_csr.public_key_sha256,
                hardware_digest,
                _canonical_json(capabilities),
                payload.agent_version,
                now_db,
                now_db,
            ),
        )
        _insert_certificate(
            connection,
            row,
            certificate_id=certificate_id,
            identity_id=identity_id,
            prior_certificate_id=None,
            generation=1,
            parsed_csr=parsed_csr,
            issued=issued,
            now=now_db,
        )
        connection.execute(
            """
            UPDATE edge_registration_intents
            SET status='consumed',csr_sha256=?,public_key_sha256=?,consumed_request_sha256=?,
              result_identity_id=?,result_certificate_id=?,consumed_at=?,updated_at=?
            WHERE edge_registration_intent_id=? AND status='issued'
            """,
            (
                parsed_csr.csr_sha256,
                parsed_csr.public_key_sha256,
                consumed_request_sha256,
                identity_id,
                certificate_id,
                now_db,
                now_db,
                row["edge_registration_intent_id"],
            ),
        )
        updated = connection.execute(
            """
            UPDATE edge_deployments
            SET status='active',revision=revision+1,agent_version=?,health_code='healthy',
              certificate_expires_at=?,updated_at=?
            WHERE edge_deployment_id=? AND status='pending' AND revision=?
            """,
            (
                payload.agent_version,
                _db_time(issued.not_after),
                now_db,
                deployment_id,
                row["deployment_revision"],
            ),
        )
        if updated.rowcount != 1:
            raise ApiError("edge.state_conflict")
        _event(
            connection,
            row,
            identity_id=identity_id,
            certificate_id=certificate_id,
            actor_type="agent",
            actor_account_id=None,
            event_type="edge.registered",
            generation=1,
            outcome="succeeded",
            reason="registered",
            request_id=request_id,
            trace_id=trace_id,
            now=now_db,
        )
        _refresh_health_if_available(
            connection,
            deployment_id,
            request_id=request_id,
            trace_id=trace_id,
            now=now,
        )
        result = _certificate_response(
            connection,
            config,
            deployment_id=deployment_id,
            identity_id=identity_id,
            certificate_id=certificate_id,
            idempotent_replay=False,
            request_id=request_id,
            trace_id=trace_id,
        )
        connection.commit()
        return result
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, DbError, KeyError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("edge.unavailable") from None
    finally:
        connection.close()


def rotate_edge_certificate(
    config: AppConfig,
    payload: EdgeCertificateRotationRequest,
    *,
    verified_fingerprint: str,
    request_id: str,
    trace_id: str,
) -> EdgeCertificateResponse:
    try:
        deployment_id = validate_edge_deployment_id(payload.deployment_id)
        rotation_nonce = validate_edge_nonce(payload.rotation_nonce)
        parsed_csr = parse_edge_csr(payload.csr_pem)
    except ValueError as exc:
        code = str(exc)
        error_code = (
            "edge.csr_invalid" if code == "edge.csr_invalid" else "edge.authentication_failed"
        )
        raise ApiError(error_code) from None
    request_sha256 = _digest(payload.model_dump(mode="json"))
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        source = _machine_certificate(connection, verified_fingerprint, deployment_id)
        nonce_hmac = _secret_hmac(
            config,
            f"edge-rotation-nonce-v1\0{verified_fingerprint}",
            rotation_nonce,
        )
        command = connection.execute(
            """
            SELECT * FROM edge_machine_commands
            WHERE edge_deployment_id=? AND source_certificate_sha256=?
              AND action_code='edge.certificate.rotate' AND nonce_hmac=?
            """,
            (deployment_id, verified_fingerprint, nonce_hmac),
        ).fetchone()
        if command is not None:
            if str(command["request_sha256"]) != request_sha256:
                raise ApiError("edge.authentication_failed")
            result = _certificate_response(
                connection,
                config,
                deployment_id=deployment_id,
                identity_id=str(command["result_identity_id"]),
                certificate_id=str(command["result_certificate_id"]),
                idempotent_replay=True,
                request_id=request_id,
                trace_id=trace_id,
            )
            connection.commit()
            return result
        now = _utcnow()
        if (
            str(source["deployment_status"]) != "active"
            or str(source["certificate_status"]) != "active"
            or str(source["identity_status"]) != "active"
            or _datetime(source["not_before"]) > now
            or _datetime(source["not_after"]) <= now
        ):
            _reject_known_agent(
                connection,
                source,
                reason="certificate_inactive",
                request_id=request_id,
                trace_id=trace_id,
                identity_id=str(source["edge_device_identity_id"]),
                certificate_id=str(source["edge_device_certificate_id"]),
            )
        if int(source["identity_generation"]) != payload.expected_generation:
            _reject_known_agent(
                connection,
                source,
                reason="generation_conflict",
                request_id=request_id,
                trace_id=trace_id,
                identity_id=str(source["edge_device_identity_id"]),
                certificate_id=str(source["edge_device_certificate_id"]),
                error_code="edge.generation_conflict",
            )
        if (
            connection.execute(
                "SELECT 1 FROM edge_device_identities WHERE public_key_sha256=?",
                (parsed_csr.public_key_sha256,),
            ).fetchone()
            is not None
        ):
            raise ApiError("edge.csr_invalid")
        generation = payload.expected_generation + 1
        identity_id = _id("edi_")
        certificate_id = _id("edc_")
        issued = _issue_certificate(
            config,
            parsed_csr,
            deployment_id=deployment_id,
            identity_id=identity_id,
            generation=generation,
            now=now,
        )
        now_db = _db_time(now)
        drain_until = _db_time(now + _ROTATION_DRAIN)
        connection.execute(
            """UPDATE edge_device_identities SET status='superseded',updated_at=?
               WHERE edge_device_identity_id=? AND status='active'""",
            (now_db, source["edge_device_identity_id"]),
        )
        connection.execute(
            """
            UPDATE edge_device_certificates
            SET status='draining',drain_until=?,updated_at=?
            WHERE edge_device_certificate_id=? AND status='active'
            """,
            (drain_until, now_db, source["edge_device_certificate_id"]),
        )
        connection.execute(
            """
            UPDATE edge_sessions SET status='revoked',
              revoked_reason_code='identity_rotated',revoked_at=?
            WHERE edge_deployment_id=? AND edge_device_identity_id=? AND status='active'
            """,
            (now_db, deployment_id, source["edge_device_identity_id"]),
        )
        connection.execute(
            """
            INSERT INTO edge_device_identities(
              edge_device_identity_id,workspace_id,tenant_id,edge_deployment_id,
              public_key_sha256,hardware_proof_sha256,capability_codes_json,agent_version,
              generation,status,created_at,updated_at,revoked_at)
            VALUES(?,?,?,?,?,?,?,?,?,'active',?,?,NULL)
            """,
            (
                identity_id,
                source["workspace_id"],
                source["tenant_id"],
                deployment_id,
                parsed_csr.public_key_sha256,
                _optional_digest(payload.hardware_proof_sha256) or source["hardware_proof_sha256"],
                source["capability_codes_json"],
                source["agent_version"],
                generation,
                now_db,
                now_db,
            ),
        )
        _insert_certificate(
            connection,
            source,
            certificate_id=certificate_id,
            identity_id=identity_id,
            prior_certificate_id=str(source["edge_device_certificate_id"]),
            generation=generation,
            parsed_csr=parsed_csr,
            issued=issued,
            now=now_db,
        )
        connection.execute(
            """
            INSERT INTO edge_machine_commands(
              edge_machine_command_id,workspace_id,tenant_id,edge_deployment_id,
              source_certificate_sha256,action_code,nonce_hmac,request_sha256,
              result_identity_id,result_certificate_id,created_at)
            VALUES(?,?,?,?,?,'edge.certificate.rotate',?,?,?,?,?)
            """,
            (
                _id("emc_"),
                source["workspace_id"],
                source["tenant_id"],
                deployment_id,
                verified_fingerprint,
                nonce_hmac,
                request_sha256,
                identity_id,
                certificate_id,
                now_db,
            ),
        )
        connection.execute(
            """
            UPDATE edge_deployments
            SET revision=revision+1,certificate_expires_at=?,health_code='healthy',updated_at=?
            WHERE edge_deployment_id=? AND status='active'
            """,
            (_db_time(issued.not_after), now_db, deployment_id),
        )
        _event(
            connection,
            source,
            identity_id=identity_id,
            certificate_id=certificate_id,
            actor_type="agent",
            actor_account_id=None,
            event_type="edge.certificate_rotated",
            generation=generation,
            outcome="succeeded",
            reason="rotated",
            request_id=request_id,
            trace_id=trace_id,
            now=now_db,
        )
        _refresh_health_if_available(
            connection,
            deployment_id,
            request_id=request_id,
            trace_id=trace_id,
            now=now,
        )
        result = _certificate_response(
            connection,
            config,
            deployment_id=deployment_id,
            identity_id=identity_id,
            certificate_id=certificate_id,
            idempotent_replay=False,
            request_id=request_id,
            trace_id=trace_id,
        )
        connection.commit()
        return result
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, DbError, KeyError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("edge.unavailable") from None
    finally:
        connection.close()


def create_edge_session(
    config: AppConfig,
    payload: EdgeSessionCreateRequest,
    *,
    verified_fingerprint: str,
    request_id: str,
    trace_id: str,
) -> EdgeSessionResponse:
    try:
        deployment_id = validate_edge_deployment_id(payload.deployment_id)
        nonce = validate_edge_nonce(payload.handshake_nonce)
    except ValueError:
        raise ApiError("edge.authentication_failed") from None
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        certificate = _machine_certificate(connection, verified_fingerprint, deployment_id)
        now = _utcnow()
        certificate_status = str(certificate["certificate_status"])
        identity_status = str(certificate["identity_status"])
        draining_valid = (
            certificate_status == "draining"
            and identity_status == "superseded"
            and certificate["drain_until"] is not None
            and _datetime(certificate["drain_until"]) > now
        )
        if (
            str(certificate["deployment_status"]) != "active"
            or not (
                certificate_status == "active" and identity_status == "active" or draining_valid
            )
            or _datetime(certificate["not_before"]) > now
            or _datetime(certificate["not_after"]) <= now
        ):
            _reject_known_agent(
                connection,
                certificate,
                reason="certificate_inactive",
                request_id=request_id,
                trace_id=trace_id,
                identity_id=str(certificate["edge_device_identity_id"]),
                certificate_id=str(certificate["edge_device_certificate_id"]),
            )
        nonce_hmac = _secret_hmac(config, f"edge-session-nonce-v1\0{deployment_id}", nonce)
        if (
            connection.execute(
                "SELECT 1 FROM edge_sessions WHERE edge_deployment_id=? AND handshake_nonce_hmac=?",
                (deployment_id, nonce_hmac),
            ).fetchone()
            is not None
        ):
            _reject_known_agent(
                connection,
                certificate,
                reason="credential_replayed",
                request_id=request_id,
                trace_id=trace_id,
                identity_id=str(certificate["edge_device_identity_id"]),
                certificate_id=str(certificate["edge_device_certificate_id"]),
            )
        session_id = _id("eds_")
        session_token = "egs_" + secrets.token_urlsafe(32)
        expires_at = now + timedelta(seconds=payload.ttl_seconds)
        now_db = _db_time(now)
        connection.execute(
            """
            INSERT INTO edge_sessions(
              edge_session_id,workspace_id,tenant_id,edge_deployment_id,
              edge_device_identity_id,edge_device_certificate_id,session_token_hmac,
              peer_binding_sha256,handshake_nonce_hmac,identity_generation,
              revocation_generation,status,expires_at,revoked_reason_code,created_at,revoked_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,'active',?,NULL,?,NULL)
            """,
            (
                session_id,
                certificate["workspace_id"],
                certificate["tenant_id"],
                deployment_id,
                certificate["edge_device_identity_id"],
                certificate["edge_device_certificate_id"],
                _secret_hmac(config, "edge-session-token-v1", session_token),
                verified_fingerprint,
                nonce_hmac,
                certificate["identity_generation"],
                certificate["revocation_generation"],
                _db_time(expires_at),
                now_db,
            ),
        )
        connection.execute(
            """UPDATE edge_deployments SET last_connected_at=?,health_code='healthy',updated_at=?
               WHERE edge_deployment_id=? AND status='active'""",
            (now_db, now_db, deployment_id),
        )
        _refresh_health_if_available(
            connection,
            deployment_id,
            request_id=request_id,
            trace_id=trace_id,
            now=now,
        )
        result = EdgeSessionResponse(
            session_id=session_id,
            deployment_id=deployment_id,
            identity_id=str(certificate["edge_device_identity_id"]),
            certificate_id=str(certificate["edge_device_certificate_id"]),
            identity_generation=int(certificate["identity_generation"]),
            revocation_generation=int(certificate["revocation_generation"]),
            session_token=session_token,
            expires_at=expires_at,
            request_id=_identifier(request_id),
            trace_id=_identifier(trace_id),
        )
        connection.commit()
        return result
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, DbError, KeyError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("edge.unavailable") from None
    finally:
        connection.close()


def revoke_edge_deployment(
    config: AppConfig,
    deployment_id: str,
    payload: EdgeDeploymentRevokeRequest,
    *,
    session_token: str,
    csrf_token: str,
    expected_revision: int,
    idempotency_key: str,
    request_id: str,
    trace_id: str,
) -> EdgeDeploymentRevokeResponse:
    try:
        checked_deployment_id = validate_edge_deployment_id(deployment_id)
    except ValueError:
        raise ApiError("edge.deployment_not_found") from None
    request_sha256 = _digest(
        {
            "deployment_id": checked_deployment_id,
            "expected_revision": expected_revision,
            "payload": payload.model_dump(mode="json"),
        }
    )
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
        idempotency_sha256 = _workspace_idempotency(config, context, "edge.revoke", idempotency_key)
        replay = _workspace_command(connection, context, "edge.revoke", idempotency_sha256)
        if replay is not None:
            if str(replay["request_sha256"]) != request_sha256:
                raise ApiError("edge.idempotency_conflict")
            deployment = _owned_deployment(connection, context, checked_deployment_id)
            revoked_sessions = int(
                connection.execute(
                    """SELECT COUNT(*) FROM edge_sessions WHERE edge_deployment_id=?
                       AND status='revoked' AND revoked_reason_code='deployment_revoked'""",
                    (checked_deployment_id,),
                ).fetchone()[0]
            )
            result = EdgeDeploymentRevokeResponse(
                deployment=_deployment_summary(connection, deployment),
                revoked_session_count=revoked_sessions,
                idempotent_replay=True,
                request_id=_identifier(request_id),
                trace_id=_identifier(trace_id),
            )
            connection.commit()
            return result
        deployment = _owned_deployment(connection, context, checked_deployment_id)
        if int(deployment["revision"]) != expected_revision:
            raise ApiError("edge.revision_conflict")
        if str(deployment["status"]) in {"revoked", "retired"}:
            raise ApiError("edge.state_conflict")
        now_db = _db_time(_utcnow())
        generation = int(deployment["revocation_generation"]) + 1
        connection.execute(
            """
            UPDATE edge_deployments
            SET status='revoked',revision=revision+1,revocation_generation=?,
              health_code='revoked',revoked_by_account_id=?,revoked_reason_code=?,
              revoked_at=?,updated_at=?
            WHERE edge_deployment_id=? AND revision=?
            """,
            (
                generation,
                context.account_id,
                payload.reason_code,
                now_db,
                now_db,
                checked_deployment_id,
                expected_revision,
            ),
        )
        connection.execute(
            """UPDATE edge_registration_intents SET status='revoked',updated_at=?
               WHERE edge_deployment_id=? AND status='issued'""",
            (now_db, checked_deployment_id),
        )
        connection.execute(
            """UPDATE edge_device_identities SET status='revoked',revoked_at=?,updated_at=?
               WHERE edge_deployment_id=? AND status!='revoked'""",
            (now_db, now_db, checked_deployment_id),
        )
        connection.execute(
            """UPDATE edge_device_certificates
               SET status='revoked',drain_until=NULL,revoked_at=?,updated_at=?
               WHERE edge_deployment_id=? AND status!='revoked'""",
            (now_db, now_db, checked_deployment_id),
        )
        revoked_sessions = connection.execute(
            """
            UPDATE edge_sessions SET status='revoked',
              revoked_reason_code='deployment_revoked',revoked_at=?
            WHERE edge_deployment_id=? AND status='active'
            """,
            (now_db, checked_deployment_id),
        ).rowcount
        latest = connection.execute(
            """
            SELECT identity.edge_device_identity_id,certificate.edge_device_certificate_id
            FROM edge_device_identities identity
            LEFT JOIN edge_device_certificates certificate
              ON certificate.edge_device_identity_id=identity.edge_device_identity_id
            WHERE identity.edge_deployment_id=?
            ORDER BY identity.generation DESC,certificate.created_at DESC LIMIT 1
            """,
            (checked_deployment_id,),
        ).fetchone()
        _event(
            connection,
            deployment,
            identity_id=(str(latest["edge_device_identity_id"]) if latest else None),
            certificate_id=(
                str(latest["edge_device_certificate_id"])
                if latest and latest["edge_device_certificate_id"] is not None
                else None
            ),
            actor_type="human",
            actor_account_id=context.account_id,
            event_type="edge.revoked",
            generation=generation,
            outcome="succeeded",
            reason=payload.reason_code,
            request_id=request_id,
            trace_id=trace_id,
            now=now_db,
        )
        _insert_workspace_command(
            connection,
            context,
            action="edge.revoke",
            idempotency_sha256=idempotency_sha256,
            request_sha256=request_sha256,
            result_object_id=checked_deployment_id,
            now=now_db,
        )
        _refresh_health_if_available(
            connection,
            checked_deployment_id,
            request_id=request_id,
            trace_id=trace_id,
            now=_datetime(now_db),
        )
        deployment = _owned_deployment(connection, context, checked_deployment_id)
        result = EdgeDeploymentRevokeResponse(
            deployment=_deployment_summary(connection, deployment),
            revoked_session_count=revoked_sessions,
            idempotent_replay=False,
            request_id=_identifier(request_id),
            trace_id=_identifier(trace_id),
        )
        connection.commit()
        return result
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, DbError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        _rollback(connection)
        raise ApiError("edge.unavailable") from None
    finally:
        connection.close()


def list_platform_edge_fleet(
    config: AppConfig,
    context: PlatformContext,
    *,
    request_id: str,
    trace_id: str,
) -> PlatformEdgeFleetResponse:
    if not {
        "platform.edge_fleet.read",
        "platform.edge_fleet.manage",
    }.intersection(context.permissions):
        raise ApiError("platform.permission_denied")
    connection = _connect(config)
    try:
        telemetry_available = (
            connection.execute(
                "SELECT 1 FROM sqlite_schema WHERE type='table' "
                "AND name='edge_fleet_telemetry_projections'"
            ).fetchone()
            is not None
        )
        telemetry_columns = (
            "telemetry.last_receipt_at telemetry_last_receipt_at,"
            "telemetry.lag_seconds telemetry_lag_seconds,"
            "telemetry.backlog_seconds telemetry_backlog_seconds,"
            "coalesce(telemetry.cpu_bucket,'unknown') telemetry_cpu_bucket,"
            "coalesce(telemetry.memory_bucket,'unknown') telemetry_memory_bucket,"
            "coalesce(telemetry.error_rate_basis_points,0) "
            "telemetry_error_rate_basis_points,"
            "coalesce(telemetry.last_failure_code,'none') telemetry_failure_code"
            if telemetry_available
            else "NULL telemetry_last_receipt_at,NULL telemetry_lag_seconds,"
            "NULL telemetry_backlog_seconds,'unknown' telemetry_cpu_bucket,"
            "'unknown' telemetry_memory_bucket,0 telemetry_error_rate_basis_points,"
            "'none' telemetry_failure_code"
        )
        telemetry_join = (
            "LEFT JOIN edge_fleet_telemetry_projections telemetry "
            "ON telemetry.edge_deployment_id=deployment.edge_deployment_id"
            if telemetry_available
            else ""
        )
        rows = connection.execute(
            f"""
            SELECT deployment.edge_deployment_id,deployment.region_code,deployment.mode,
              deployment.status,deployment.revision,deployment.revocation_generation,
              deployment.agent_version,deployment.health_code,
              deployment.certificate_expires_at,deployment.last_connected_at,
              deployment.updated_at,coalesce(head.generation,0) desired_generation,
              coalesce(head.applied_generation,0) applied_generation,
              coalesce(head.lkg_generation,0) lkg_generation,
              coalesce(head.last_ack_status,'not_published') desired_state_status,
              coalesce(head.last_reason_code,'not_published') desired_state_reason_code,
              {telemetry_columns}
            FROM edge_deployments deployment
            LEFT JOIN edge_desired_state_heads head
              ON head.edge_deployment_id=deployment.edge_deployment_id
            {telemetry_join}
            ORDER BY deployment.updated_at DESC,deployment.edge_deployment_id DESC
            """
        ).fetchall()
        return PlatformEdgeFleetResponse(
            deployments=tuple(
                PlatformEdgeFleetSummary(
                    deployment_id=str(row["edge_deployment_id"]),
                    region_code=str(row["region_code"]),
                    mode=str(row["mode"]),
                    status=str(row["status"]),
                    revision=int(row["revision"]),
                    revocation_generation=int(row["revocation_generation"]),
                    agent_version=row["agent_version"],
                    health_code=str(row["health_code"]),
                    certificate_expires_at=_optional_datetime(row["certificate_expires_at"]),
                    last_connected_at=_optional_datetime(row["last_connected_at"]),
                    desired_generation=int(row["desired_generation"]),
                    applied_generation=int(row["applied_generation"]),
                    lkg_generation=int(row["lkg_generation"]),
                    desired_state_status=str(row["desired_state_status"]),
                    desired_state_reason_code=str(row["desired_state_reason_code"]),
                    desired_state_drifted=(
                        int(row["desired_generation"]) != int(row["applied_generation"])
                    ),
                    telemetry_last_receipt_at=_optional_datetime(
                        row["telemetry_last_receipt_at"]
                    ),
                    telemetry_lag_seconds=(
                        int(row["telemetry_lag_seconds"])
                        if row["telemetry_lag_seconds"] is not None
                        else None
                    ),
                    telemetry_backlog_seconds=(
                        int(row["telemetry_backlog_seconds"])
                        if row["telemetry_backlog_seconds"] is not None
                        else None
                    ),
                    telemetry_cpu_bucket=str(row["telemetry_cpu_bucket"]),
                    telemetry_memory_bucket=str(row["telemetry_memory_bucket"]),
                    telemetry_error_rate_basis_points=int(
                        row["telemetry_error_rate_basis_points"]
                    ),
                    telemetry_failure_code=str(row["telemetry_failure_code"]),
                    updated_at=_datetime(row["updated_at"]),
                )
                for row in rows
            ),
            request_id=_identifier(request_id),
            trace_id=_identifier(trace_id),
        )
    except ApiError:
        raise
    except (sqlite3.Error, DbError, KeyError, TypeError, ValueError):
        raise ApiError("edge.unavailable") from None
    finally:
        connection.close()


class _IssuedCertificate:
    __slots__ = ("certificate_pem", "certificate_sha256", "not_after", "not_before", "serial_hex")

    def __init__(self, certificate: x509.Certificate) -> None:
        self.certificate_pem = certificate.public_bytes(serialization.Encoding.PEM).decode("ascii")
        self.certificate_sha256 = certificate_sha256(certificate)
        self.serial_hex = format(certificate.serial_number, "x")
        self.not_before = certificate.not_valid_before_utc
        self.not_after = certificate.not_valid_after_utc


def _issue_certificate(
    config: AppConfig,
    parsed_csr: EdgeCsr,
    *,
    deployment_id: str,
    identity_id: str,
    generation: int,
    now: datetime,
) -> _IssuedCertificate:
    private_key, ca_certificate = _ca_material(config)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(
            x509.Name(
                [
                    x509.NameAttribute(NameOID.ORGANIZATION_NAME, "MetaHub Edge"),
                    x509.NameAttribute(NameOID.COMMON_NAME, identity_id),
                ]
            )
        )
        .issuer_name(ca_certificate.subject)
        .public_key(parsed_csr.public_key)
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + _CERTIFICATE_TTL)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]), critical=True)
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.UniformResourceIdentifier(
                        f"spiffe://metahub.local/edge/{deployment_id}/{identity_id}/{generation}"
                    )
                ]
            ),
            critical=False,
        )
        .sign(private_key=private_key, algorithm=None)
    )
    return _IssuedCertificate(certificate)


def _ca_material(config: AppConfig) -> tuple[Ed25519PrivateKey, x509.Certificate]:
    seed = hmac.new(
        config.tenant_data_encryption_secret.encode("utf-8"),
        b"metahub/edge-device-ca/v1",
        hashlib.sha256,
    ).digest()
    private_key = Ed25519PrivateKey.from_private_bytes(seed)
    subject = x509.Name(
        [
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "MetaHub"),
            x509.NameAttribute(NameOID.COMMON_NAME, "MetaHub Edge Device CA v1"),
        ]
    )
    serial = int.from_bytes(hashlib.sha256(b"metahub-edge-device-ca-v1").digest()[:19], "big")
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(private_key.public_key())
        .serial_number(serial)
        .not_valid_before(datetime(2026, 1, 1, tzinfo=UTC))
        .not_valid_after(datetime(2036, 1, 1, tzinfo=UTC))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(private_key.public_key()), critical=False
        )
        .sign(private_key=private_key, algorithm=None)
    )
    return private_key, certificate


def _ca_pem(config: AppConfig) -> str:
    return _ca_material(config)[1].public_bytes(serialization.Encoding.PEM).decode("ascii")


def _insert_certificate(
    connection: sqlite3.Connection,
    owner: sqlite3.Row,
    *,
    certificate_id: str,
    identity_id: str,
    prior_certificate_id: str | None,
    generation: int,
    parsed_csr: EdgeCsr,
    issued: _IssuedCertificate,
    now: str,
) -> None:
    connection.execute(
        """
        INSERT INTO edge_device_certificates(
          edge_device_certificate_id,workspace_id,tenant_id,edge_deployment_id,
          edge_device_identity_id,prior_certificate_id,serial_hex,certificate_sha256,
          public_key_sha256,csr_sha256,certificate_pem,issuer_key_id,generation,status,
          not_before,not_after,drain_until,created_at,updated_at,revoked_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,'edge-device-ca-v1',?,'active',?,?,NULL,?,?,NULL)
        """,
        (
            certificate_id,
            owner["workspace_id"],
            owner["tenant_id"],
            owner["edge_deployment_id"],
            identity_id,
            prior_certificate_id,
            issued.serial_hex,
            issued.certificate_sha256,
            parsed_csr.public_key_sha256,
            parsed_csr.csr_sha256,
            issued.certificate_pem,
            generation,
            _db_time(issued.not_before),
            _db_time(issued.not_after),
            now,
            now,
        ),
    )


def _certificate_response(
    connection: sqlite3.Connection,
    config: AppConfig,
    *,
    deployment_id: str,
    identity_id: str,
    certificate_id: str,
    idempotent_replay: bool,
    request_id: str,
    trace_id: str,
) -> EdgeCertificateResponse:
    row = connection.execute(
        """
        SELECT identity.*,certificate.*
        FROM edge_device_identities identity
        JOIN edge_device_certificates certificate
          ON certificate.edge_device_identity_id=identity.edge_device_identity_id
         AND certificate.edge_deployment_id=identity.edge_deployment_id
        WHERE identity.edge_deployment_id=? AND identity.edge_device_identity_id=?
          AND certificate.edge_device_certificate_id=?
        """,
        (deployment_id, identity_id, certificate_id),
    ).fetchone()
    if row is None:
        raise ApiError("edge.unavailable")
    return EdgeCertificateResponse(
        deployment_id=deployment_id,
        identity=_identity_summary(row),
        certificate=_certificate_summary(row),
        certificate_pem=str(row["certificate_pem"]),
        ca_certificate_pem=_ca_pem(config),
        permissions=_AGENT_PERMISSIONS,
        idempotent_replay=idempotent_replay,
        request_id=_identifier(request_id),
        trace_id=_identifier(trace_id),
    )


def _machine_certificate(
    connection: sqlite3.Connection, fingerprint: str, deployment_id: str
) -> sqlite3.Row:
    if _HEX_64_RE.fullmatch(fingerprint) is None:
        raise ApiError("edge.authentication_failed")
    row = connection.execute(
        """
        SELECT certificate.status certificate_status,identity.status identity_status,
          identity.generation identity_generation,deployment.status deployment_status,
          deployment.revocation_generation,deployment.revision deployment_revision,
          deployment.*,identity.*,certificate.*
        FROM edge_device_certificates certificate
        JOIN edge_device_identities identity
          ON identity.edge_device_identity_id=certificate.edge_device_identity_id
         AND identity.edge_deployment_id=certificate.edge_deployment_id
        JOIN edge_deployments deployment
          ON deployment.edge_deployment_id=certificate.edge_deployment_id
        WHERE certificate.certificate_sha256=?
        """,
        (fingerprint,),
    ).fetchone()
    if row is None or not hmac.compare_digest(str(row["edge_deployment_id"]), deployment_id):
        raise ApiError("edge.authentication_failed")
    return row


def _workspace_context(
    connection: sqlite3.Connection,
    config: AppConfig,
    *,
    session_token: str,
    csrf_token: str | None,
    permission: str,
) -> WorkspaceSessionContext:
    context = authenticate_workspace_session_in_transaction(
        connection,
        config,
        session_token=session_token,
        csrf_token=csrf_token,
        touch=csrf_token is not None,
    )
    if permission not in context.permission_codes:
        raise ApiError("workspace_session.permission_denied")
    return context


def _owned_deployment(
    connection: sqlite3.Connection, context: WorkspaceSessionContext, deployment_id: str
) -> sqlite3.Row:
    row = connection.execute(
        """SELECT * FROM edge_deployments
           WHERE workspace_id=? AND tenant_id=? AND edge_deployment_id=?""",
        (context.workspace_id, context.tenant_id, deployment_id),
    ).fetchone()
    if row is None:
        raise ApiError("edge.deployment_not_found")
    return row


def _workspace_command(
    connection: sqlite3.Connection,
    context: WorkspaceSessionContext,
    action: str,
    idempotency_sha256: str,
) -> sqlite3.Row | None:
    return connection.execute(
        """
        SELECT * FROM workspace_edge_commands
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


def _insert_workspace_command(
    connection: sqlite3.Connection,
    context: WorkspaceSessionContext,
    *,
    action: str,
    idempotency_sha256: str,
    request_sha256: str,
    result_object_id: str,
    now: str,
) -> None:
    connection.execute(
        """
        INSERT INTO workspace_edge_commands(
          workspace_edge_command_id,workspace_id,tenant_id,actor_account_id,action_code,
          idempotency_sha256,request_sha256,result_object_id,created_at)
        VALUES(?,?,?,?,?,?,?,?,?)
        """,
        (
            _id("egc_"),
            context.workspace_id,
            context.tenant_id,
            context.account_id,
            action,
            idempotency_sha256,
            request_sha256,
            result_object_id,
            now,
        ),
    )


def _deployment_summary(connection: sqlite3.Connection, row: sqlite3.Row) -> EdgeDeploymentSummary:
    certificate = connection.execute(
        """
        SELECT * FROM edge_device_certificates
        WHERE edge_deployment_id=? AND status IN('active','draining')
        ORDER BY CASE status WHEN 'active' THEN 0 ELSE 1 END,generation DESC LIMIT 1
        """,
        (row["edge_deployment_id"],),
    ).fetchone()
    latest_identity = connection.execute(
        """SELECT generation FROM edge_device_identities
           WHERE edge_deployment_id=? ORDER BY generation DESC LIMIT 1""",
        (row["edge_deployment_id"],),
    ).fetchone()
    event_rows = connection.execute(
        """
        SELECT * FROM edge_identity_events WHERE workspace_id=? AND tenant_id=?
          AND edge_deployment_id=? ORDER BY occurred_at DESC,edge_identity_event_id DESC LIMIT 12
        """,
        (row["workspace_id"], row["tenant_id"], row["edge_deployment_id"]),
    ).fetchall()
    return EdgeDeploymentSummary(
        deployment_id=str(row["edge_deployment_id"]),
        workspace_id=str(row["workspace_id"]),
        display_name=str(row["display_name"]),
        region_code=str(row["region_code"]),
        mode=str(row["mode"]),
        status=str(row["status"]),
        revision=int(row["revision"]),
        revocation_generation=int(row["revocation_generation"]),
        agent_version=row["agent_version"],
        health_code=str(row["health_code"]),
        last_connected_at=_optional_datetime(row["last_connected_at"]),
        certificate_expires_at=_optional_datetime(row["certificate_expires_at"]),
        identity_generation=(int(latest_identity["generation"]) if latest_identity else None),
        current_certificate=(_certificate_summary(certificate) if certificate else None),
        recent_events=tuple(_event_summary(item) for item in reversed(event_rows)),
        created_at=_datetime(row["created_at"]),
        updated_at=_datetime(row["updated_at"]),
        revoked_at=_optional_datetime(row["revoked_at"]),
    )


def _identity_summary(row: sqlite3.Row) -> EdgeDeviceIdentitySummary:
    capabilities = json.loads(str(row["capability_codes_json"]))
    if type(capabilities) is not list or any(type(item) is not str for item in capabilities):
        raise ValueError("invalid capabilities")
    return EdgeDeviceIdentitySummary(
        identity_id=str(row["edge_device_identity_id"]),
        deployment_id=str(row["edge_deployment_id"]),
        public_key_sha256=str(row["public_key_sha256"]),
        hardware_proof_sha256=row["hardware_proof_sha256"],
        capability_codes=tuple(capabilities),
        agent_version=str(row["agent_version"]),
        generation=int(row["generation"]),
        status=str(row["status"]),
        created_at=_datetime(row["created_at"]),
    )


def _certificate_summary(row: sqlite3.Row) -> EdgeCertificateSummary:
    return EdgeCertificateSummary(
        certificate_id=str(row["edge_device_certificate_id"]),
        identity_id=str(row["edge_device_identity_id"]),
        generation=int(row["generation"]),
        status=str(row["status"]),
        certificate_sha256=str(row["certificate_sha256"]),
        public_key_sha256=str(row["public_key_sha256"]),
        issuer_key_id=str(row["issuer_key_id"]),
        not_before=_datetime(row["not_before"]),
        not_after=_datetime(row["not_after"]),
        drain_until=_optional_datetime(row["drain_until"]),
    )


def _event_summary(row: sqlite3.Row) -> EdgeEventSummary:
    return EdgeEventSummary(
        event_id=str(row["edge_identity_event_id"]),
        event_type=str(row["event_type"]),
        generation=int(row["generation"]),
        outcome_code=str(row["outcome_code"]),
        reason_code=str(row["reason_code"]),
        occurred_at=_datetime(row["occurred_at"]),
    )


def _event(
    connection: sqlite3.Connection,
    owner: sqlite3.Row,
    *,
    identity_id: str | None,
    certificate_id: str | None,
    actor_type: str,
    actor_account_id: str | None,
    event_type: str,
    generation: int,
    outcome: str,
    reason: str,
    request_id: str,
    trace_id: str,
    now: str,
) -> None:
    event_id = _id("ege_")
    connection.execute(
        """
        INSERT INTO edge_identity_events(
          edge_identity_event_id,workspace_id,tenant_id,edge_deployment_id,
          edge_device_identity_id,edge_device_certificate_id,actor_type,actor_account_id,
          event_type,generation,outcome_code,reason_code,request_id,trace_id,occurred_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            event_id,
            owner["workspace_id"],
            owner["tenant_id"],
            owner["edge_deployment_id"],
            identity_id,
            certificate_id,
            actor_type,
            actor_account_id,
            event_type,
            generation,
            outcome,
            reason,
            _identifier(request_id),
            _identifier(trace_id),
            now,
        ),
    )
    domain = "security" if event_type == "edge.authentication_failed" else "fleet"
    connection.execute(
        """
        INSERT INTO edge_identity_event_outbox(
          edge_identity_outbox_id,edge_identity_event_id,delivery_domain,status,
          attempt_count,available_at,delivered_at,last_error_code,created_at,updated_at)
        VALUES(?,?,?,'pending',0,?,NULL,NULL,?,?)
        """,
        (_id("ego_"), event_id, domain, now, now, now),
    )


def _reject_known_agent(
    connection: sqlite3.Connection,
    owner: sqlite3.Row,
    *,
    reason: str,
    request_id: str,
    trace_id: str,
    identity_id: str | None = None,
    certificate_id: str | None = None,
    error_code: str = "edge.authentication_failed",
) -> None:
    now = _db_time(_utcnow())
    _event(
        connection,
        owner,
        identity_id=identity_id,
        certificate_id=certificate_id,
        actor_type="agent",
        actor_account_id=None,
        event_type="edge.authentication_failed",
        generation=int(owner["revocation_generation"]),
        outcome="rejected",
        reason=reason,
        request_id=request_id,
        trace_id=trace_id,
        now=now,
    )
    connection.commit()
    raise ApiError(error_code)


def _workspace_idempotency(
    config: AppConfig,
    context: WorkspaceSessionContext,
    action: str,
    value: str,
) -> str:
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{15,127}", value) is None:
        raise ApiError("edge.request_invalid")
    return hmac.new(
        config.workspace_session_secret.encode("utf-8"),
        (
            "prd120/workspace-idempotency\0"
            f"{context.workspace_id}\0{context.account_id}\0{action}\0{value}"
        ).encode(),
        hashlib.sha256,
    ).hexdigest()


def _insert_default_telemetry_policy_if_available(
    connection: sqlite3.Connection,
    context: WorkspaceSessionContext,
    *,
    deployment_id: str,
    now: str,
) -> None:
    if (
        connection.execute(
            "SELECT 1 FROM sqlite_schema WHERE type='table' "
            "AND name='edge_telemetry_policies'"
        ).fetchone()
        is None
    ):
        return
    policy_id = _id("etp_")
    request_sha256 = _digest(
        {"deployment_id": deployment_id, "source": "system_default", "level": "minimal"}
    )
    connection.execute(
        """
        INSERT INTO edge_telemetry_policies(
          edge_telemetry_policy_id,workspace_id,tenant_id,edge_deployment_id,revision,
          telemetry_level,source_code,actor_account_id,idempotency_sha256,
          request_sha256,created_at)
        VALUES(?,?,?,?,1,'minimal','system_default',?,NULL,?,?)
        """,
        (
            policy_id,
            context.workspace_id,
            context.tenant_id,
            deployment_id,
            context.account_id,
            request_sha256,
            now,
        ),
    )
    connection.execute(
        """
        INSERT INTO edge_telemetry_policy_heads(
          edge_deployment_id,workspace_id,tenant_id,edge_telemetry_policy_id,
          revision,telemetry_level,updated_at)
        VALUES(?,?,?,?,1,'minimal',?)
        """,
        (
            deployment_id,
            context.workspace_id,
            context.tenant_id,
            policy_id,
            now,
        ),
    )


def _refresh_health_if_available(
    connection: sqlite3.Connection,
    deployment_id: str,
    *,
    request_id: str,
    trace_id: str,
    now: datetime,
) -> None:
    if (
        connection.execute(
            "SELECT 1 FROM sqlite_schema WHERE type='table' AND name='edge_health_snapshots'"
        ).fetchone()
        is None
    ):
        return
    from .fleet_diagnostic_service import refresh_edge_health_projection_in_transaction

    refresh_edge_health_projection_in_transaction(
        connection,
        deployment_id,
        request_id=request_id,
        trace_id=trace_id,
        now=now,
    )


def _secret_hmac(config: AppConfig, purpose: str, value: str) -> str:
    return hmac.new(
        config.workspace_session_secret.encode("utf-8"),
        f"prd120/{purpose}\0{value}".encode(),
        hashlib.sha256,
    ).hexdigest()


def _capabilities(values: list[str]) -> list[str]:
    checked = sorted(set(values))
    if (
        len(checked) != len(values)
        or len(checked) > 12
        or any(_CAPABILITY_RE.fullmatch(item) is None for item in checked)
        or not set(checked) <= _ALLOWED_CAPABILITIES
    ):
        raise ApiError("edge.request_invalid")
    return checked


def _agent_version(value: str) -> str:
    if _AGENT_VERSION_RE.fullmatch(value) is None:
        raise ApiError("edge.request_invalid")
    return value


def _region(value: str) -> str:
    if _REGION_RE.fullmatch(value) is None:
        raise ApiError("edge.request_invalid")
    return value


def _optional_digest(value: str | None) -> str | None:
    if value is not None and _HEX_64_RE.fullmatch(value) is None:
        raise ApiError("edge.request_invalid")
    return value


def _canonical_json(value: object) -> str:
    return canonical_json_bytes(value).decode("ascii")


def _digest(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _id(prefix: str) -> str:
    return prefix + uuid4().hex


def _identifier(value: str) -> str:
    if type(value) is not str or not 1 <= len(value) <= 160:
        raise ApiError("edge.unavailable")
    return value


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _db_time(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _datetime(value: object) -> datetime:
    if type(value) is not str:
        raise ValueError("invalid timestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("invalid timestamp")
    return parsed.astimezone(UTC)


def _optional_datetime(value: object) -> datetime | None:
    return None if value is None else _datetime(value)


def _connect(config: AppConfig) -> sqlite3.Connection:
    try:
        return connect_database(config.database_url)
    except DbError:
        raise ApiError("edge.unavailable") from None


def _rollback(connection: sqlite3.Connection) -> None:
    try:
        if connection.in_transaction:
            connection.rollback()
    except Exception:
        pass


__all__ = (
    "create_edge_session",
    "create_workspace_registration",
    "list_platform_edge_fleet",
    "list_workspace_deployments",
    "register_edge_agent",
    "revoke_edge_deployment",
    "rotate_edge_certificate",
)
