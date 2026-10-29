"""Database-authoritative enterprise domain, SSO policy, and federation services."""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import sqlite3
import zlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode
from uuid import uuid4

import httpx
from lxml import etree
from metahub_shared.db.connection import connect_database
from metahub_shared.db.errors import DbError
from metahub_shared.enterprise_identity import (
    EnterpriseSsoCryptoError,
    EnterpriseSsoSecretOwner,
    decrypt_enterprise_sso_secret,
    encrypt_enterprise_sso_secret,
    enterprise_sso_hmac,
)
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError

from metahub_control.identity.service import (
    issue_global_session_in_transaction,
    normalize_global_email,
)
from metahub_control.workspace_access.service import (
    WorkspaceSessionContext,
    authenticate_workspace_session_in_transaction,
    issue_federated_workspace_session_in_transaction,
)

from .models import (
    BreakGlassCandidateSummary,
    DomainCreateRequest,
    DomainCreateResponse,
    DomainResponse,
    EnterpriseIdentitySummaryResponse,
    LoginDiscoveryResponse,
    SsoCallbackRequest,
    SsoCallbackResponse,
    SsoCertificateSummary,
    SsoConnectionCreateRequest,
    SsoConnectionResponse,
    SsoConnectionSummary,
    SsoPolicyResponse,
    SsoPolicySummary,
    SsoPolicyUpdateRequest,
    SsoTestRequest,
    SsoTestResponse,
    VerifiedDomainSummary,
)
from .protocols import (
    ProtocolIdentity,
    exchange_oidc_code,
    normalize_enterprise_domain,
    oidc_authorization_url,
    parse_saml_certificate,
    saml_response_request_id,
    validate_fixed_https_endpoint,
    validate_oidc_id_token,
    validate_oidc_jwks,
    validate_saml_response,
)

_READ_ROLES = frozenset({"owner", "security_admin", "auditor"})
_MANAGE_ROLES = frozenset({"owner", "security_admin"})
_STATE_TTL = timedelta(minutes=5)
_DOMAIN_CHALLENGE_TTL = timedelta(hours=24)
_CERTIFICATE_MIN_REMAINING = timedelta(days=7)


@dataclass(frozen=True, slots=True)
class FederatedSessionBundle:
    response: SsoCallbackResponse
    global_session_token: str
    global_csrf_token: str
    workspace_session_token: str
    workspace_csrf_token: str


def get_enterprise_identity_summary(
    config: AppConfig,
    *,
    session_token: str,
    workspace_id: str,
    request_id: str,
    trace_id: str,
) -> EnterpriseIdentitySummaryResponse:
    connection = _connect(config)
    try:
        connection.execute("BEGIN")
        context = _authenticate(
            connection,
            config,
            session_token=session_token,
            csrf_token=None,
            workspace_id=workspace_id,
            manage=False,
        )
        domains = tuple(
            _domain_summary(row)
            for row in connection.execute(
                """SELECT * FROM verified_domains
                     WHERE workspace_id=? AND tenant_id=?
                     ORDER BY created_at,verified_domain_id""",
                (context.workspace_id, context.tenant_id),
            )
        )
        connection_rows = connection.execute(
            """SELECT * FROM sso_connections
                 WHERE workspace_id=? AND tenant_id=?
                 ORDER BY created_at,sso_connection_id""",
            (context.workspace_id, context.tenant_id),
        ).fetchall()
        connections = tuple(_connection_summary(connection, row) for row in connection_rows)
        policy_row = connection.execute(
            "SELECT * FROM sso_policies WHERE workspace_id=? AND tenant_id=?",
            (context.workspace_id, context.tenant_id),
        ).fetchone()
        candidates = tuple(
            BreakGlassCandidateSummary(
                account_id=row["account_id"],
                display_name=row["display_name"],
                password_configured=bool(row["password_configured"]),
                mfa_configured=bool(row["mfa_configured"]),
            )
            for row in connection.execute(
                """SELECT member.account_id,user.display_name,
                          EXISTS(SELECT 1 FROM account_credentials credential
                            WHERE credential.account_id=member.account_id
                              AND credential.credential_type='password'
                              AND credential.status='active') password_configured,
                          EXISTS(SELECT 1 FROM account_mfa_factors factor
                            WHERE factor.account_id=member.account_id
                              AND factor.status='active') mfa_configured
                     FROM workspace_memberships member
                     JOIN users user
                       ON user.tenant_id=member.tenant_id
                      AND user.user_id=member.legacy_user_id
                    WHERE member.workspace_id=? AND member.tenant_id=?
                      AND member.role_code='owner' AND member.status='active'
                    ORDER BY user.display_name,member.account_id""",
                (context.workspace_id, context.tenant_id),
            )
        )
        result = EnterpriseIdentitySummaryResponse(
            workspace_id=context.workspace_id,
            domains=domains,
            connections=connections,
            policy=_policy_summary(policy_row) if policy_row is not None else None,
            break_glass_candidates=candidates,
            request_id=request_id,
            trace_id=trace_id,
        )
        connection.commit()
        return result
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, DbError, ValueError, TypeError, KeyError):
        _rollback(connection)
        raise ApiError("sso.unavailable") from None
    finally:
        connection.close()


def create_verified_domain(
    config: AppConfig,
    *,
    session_token: str,
    csrf_token: str,
    workspace_id: str,
    idempotency_key: str,
    payload: DomainCreateRequest,
    request_id: str,
    trace_id: str,
) -> DomainCreateResponse:
    domain = normalize_enterprise_domain(payload.domain)
    request_sha256 = _request_sha256({"domain": domain})
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        context = _authenticate(
            connection,
            config,
            session_token=session_token,
            csrf_token=csrf_token,
            workspace_id=workspace_id,
            manage=True,
        )
        replay = _command_replay(
            connection,
            config,
            context=context,
            action="domain.create",
            idempotency_key=idempotency_key,
            request_sha256=request_sha256,
        )
        if replay is not None:
            row = _owned_domain(connection, context, replay["result_id"])
            if row is None:
                raise ApiError("sso.unavailable")
            result = DomainCreateResponse(
                domain=_domain_summary(row),
                verification_record=_domain_verification_record(
                    config, row["verified_domain_id"]
                ),
                idempotent_replay=True,
                request_id=request_id,
                trace_id=trace_id,
            )
            connection.commit()
            return result
        now = _utcnow()
        domain_id = _id("vdm")
        record = _domain_verification_record(config, domain_id)
        connection.execute(
            """INSERT INTO verified_domains(
                 verified_domain_id,workspace_id,tenant_id,domain_normalized,domain_hmac,
                 challenge_hash,status,revision,challenge_expires_at,verified_at,suspended_at,
                 created_by_account_id,updated_by_account_id,created_at,updated_at)
               VALUES(?,?,?,?,?,?,'pending',1,?,NULL,NULL,?,?,?,?)""",
            (
                domain_id,
                context.workspace_id,
                context.tenant_id,
                domain,
                enterprise_sso_hmac(config.enterprise_sso_hmac_secret, "domain", domain),
                hashlib.sha256(record.encode("ascii")).hexdigest(),
                _db_time(now + _DOMAIN_CHALLENGE_TTL),
                context.account_id,
                context.account_id,
                _db_time(now),
                _db_time(now),
            ),
        )
        _insert_command(
            connection,
            config,
            context=context,
            action="domain.create",
            idempotency_key=idempotency_key,
            request_sha256=request_sha256,
            result_type="domain",
            result_id=domain_id,
            outcome="domain_pending",
            now=now,
        )
        row = _owned_domain(connection, context, domain_id)
        if row is None:
            raise ApiError("sso.unavailable")
        result = DomainCreateResponse(
            domain=_domain_summary(row),
            verification_record=record,
            request_id=request_id,
            trace_id=trace_id,
        )
        connection.commit()
        return result
    except sqlite3.IntegrityError as exc:
        _rollback(connection)
        if "UNIQUE" in str(exc):
            raise ApiError("sso.domain_conflict") from None
        raise ApiError("sso.conflict") from None
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, DbError, EnterpriseSsoCryptoError, ValueError, TypeError, KeyError):
        _rollback(connection)
        raise ApiError("sso.unavailable") from None
    finally:
        connection.close()


def verify_domain(
    config: AppConfig,
    *,
    session_token: str,
    csrf_token: str,
    workspace_id: str,
    domain_id: str,
    expected_revision: int,
    idempotency_key: str,
    request_id: str,
    trace_id: str,
) -> DomainResponse:
    initial = _read_domain_for_external_check(
        config,
        session_token=session_token,
        csrf_token=csrf_token,
        workspace_id=workspace_id,
        domain_id=domain_id,
        expected_revision=expected_revision,
    )
    record = _domain_verification_record(config, domain_id)
    records = _lookup_txt_records(initial["domain_normalized"])
    if record not in records:
        raise ApiError("sso.domain_verification_failed")
    request_sha256 = _request_sha256(
        {"domain_id": domain_id, "revision": expected_revision, "action": "verify"}
    )
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        context = _authenticate(
            connection,
            config,
            session_token=session_token,
            csrf_token=csrf_token,
            workspace_id=workspace_id,
            manage=True,
        )
        replay = _command_replay(
            connection,
            config,
            context=context,
            action="domain.verify",
            idempotency_key=idempotency_key,
            request_sha256=request_sha256,
        )
        if replay is not None:
            row = _owned_domain(connection, context, replay["result_id"])
            if row is None:
                raise ApiError("sso.unavailable")
            result = DomainResponse(
                domain=_domain_summary(row),
                idempotent_replay=True,
                request_id=request_id,
                trace_id=trace_id,
            )
            connection.commit()
            return result
        row = _owned_domain(connection, context, domain_id)
        if row is None:
            raise ApiError("sso.not_found")
        if row["status"] != "pending" or row["revision"] != expected_revision:
            raise ApiError("sso.revision_conflict")
        if _parse_time(row["challenge_expires_at"]) <= _utcnow() or not secrets.compare_digest(
            row["challenge_hash"], hashlib.sha256(record.encode("ascii")).hexdigest()
        ):
            raise ApiError("sso.domain_verification_failed")
        now = _utcnow()
        stamp = _later_stamp(row["updated_at"], now)
        updated = connection.execute(
            """UPDATE verified_domains
                 SET status='verified',revision=revision+1,verified_at=?,
                     updated_by_account_id=?,updated_at=?
                 WHERE verified_domain_id=? AND workspace_id=? AND tenant_id=?
                   AND status='pending' AND revision=?""",
            (
                stamp,
                context.account_id,
                stamp,
                domain_id,
                context.workspace_id,
                context.tenant_id,
                expected_revision,
            ),
        ).rowcount
        if updated != 1:
            raise ApiError("sso.revision_conflict")
        _insert_event(
            connection,
            context=context,
            event_type="domain.verified",
            subject_type="domain",
            subject_id=domain_id,
            subject_revision=expected_revision + 1,
            outcome="domain_verified",
            request_id=request_id,
            trace_id=trace_id,
            now=now,
        )
        _insert_command(
            connection,
            config,
            context=context,
            action="domain.verify",
            idempotency_key=idempotency_key,
            request_sha256=request_sha256,
            result_type="domain",
            result_id=domain_id,
            outcome="domain_verified",
            now=now,
        )
        row = _owned_domain(connection, context, domain_id)
        if row is None:
            raise ApiError("sso.unavailable")
        result = DomainResponse(
            domain=_domain_summary(row), request_id=request_id, trace_id=trace_id
        )
        connection.commit()
        return result
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, DbError, EnterpriseSsoCryptoError, ValueError, TypeError, KeyError):
        _rollback(connection)
        raise ApiError("sso.unavailable") from None
    finally:
        connection.close()


def create_sso_connection(
    config: AppConfig,
    *,
    session_token: str,
    csrf_token: str,
    workspace_id: str,
    idempotency_key: str,
    payload: SsoConnectionCreateRequest,
    request_id: str,
    trace_id: str,
) -> SsoConnectionResponse:
    normalized = _connection_material(config, payload)
    request_sha256 = _request_sha256(
        {
            "verified_domain_id": payload.verified_domain_id,
            "connection_code": payload.connection_code,
            "display_name": payload.display_name,
            "protocol": payload.protocol,
            "issuer": payload.issuer,
            "public_config": normalized["public_config"],
            "certificate_fingerprints": normalized["fingerprints"],
        }
    )
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        context = _authenticate(
            connection,
            config,
            session_token=session_token,
            csrf_token=csrf_token,
            workspace_id=workspace_id,
            manage=True,
        )
        replay = _command_replay(
            connection,
            config,
            context=context,
            action="connection.create",
            idempotency_key=idempotency_key,
            request_sha256=request_sha256,
        )
        if replay is not None:
            row = _owned_connection(connection, context, replay["result_id"])
            if row is None:
                raise ApiError("sso.unavailable")
            result = SsoConnectionResponse(
                connection=_connection_summary(connection, row),
                idempotent_replay=True,
                request_id=request_id,
                trace_id=trace_id,
            )
            connection.commit()
            return result
        domain = _owned_domain(connection, context, payload.verified_domain_id)
        if domain is None:
            raise ApiError("sso.not_found")
        if domain["status"] != "verified":
            raise ApiError("sso.domain_unverified")
        now = _utcnow()
        connection_id = _id("ssc")
        ciphertext = None
        if payload.protocol == "oidc":
            ciphertext = encrypt_enterprise_sso_secret(
                payload.client_secret,
                secret=config.enterprise_sso_encryption_secret,
                owner=EnterpriseSsoSecretOwner(
                    context.workspace_id,
                    context.tenant_id,
                    connection_id,
                    "oidc_client_secret",
                ),
            )
        public_json = _canonical_json(normalized["public_config"])
        metadata_sha256 = hashlib.sha256(
            _canonical_json(
                {
                    "public_config": normalized["public_config"],
                    "fingerprints": normalized["fingerprints"],
                }
            ).encode("utf-8")
        ).hexdigest()
        connection.execute(
            """INSERT INTO sso_connections(
                 sso_connection_id,workspace_id,tenant_id,verified_domain_id,
                 connection_code,display_name,protocol,issuer,public_config_json,
                 client_secret_ciphertext,secret_version,metadata_sha256,status,revision,
                 tested_at,disabled_at,created_by_account_id,updated_by_account_id,
                 created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?, 'draft',1,NULL,NULL,?,?,?,?)""",
            (
                connection_id,
                context.workspace_id,
                context.tenant_id,
                payload.verified_domain_id,
                payload.connection_code,
                payload.display_name,
                payload.protocol,
                payload.issuer,
                public_json,
                ciphertext,
                1 if ciphertext is not None else None,
                metadata_sha256,
                context.account_id,
                context.account_id,
                _db_time(now),
                _db_time(now),
            ),
        )
        for item in normalized["certificates"]:
            connection.execute(
                """INSERT INTO sso_certificates(
                     sso_certificate_id,sso_connection_id,workspace_id,tenant_id,purpose,
                     fingerprint_sha256,public_material,status,not_before,not_after,
                     activated_at,retired_at,created_at)
                   VALUES(?,?,?,?,?,?,?,'staged',?,?,NULL,NULL,?)""",
                (
                    _id("sct"),
                    connection_id,
                    context.workspace_id,
                    context.tenant_id,
                    item["purpose"],
                    item["fingerprint"],
                    item["material"],
                    _db_time(item["not_before"]),
                    _db_time(item["not_after"]),
                    _db_time(now),
                ),
            )
        _insert_command(
            connection,
            config,
            context=context,
            action="connection.create",
            idempotency_key=idempotency_key,
            request_sha256=request_sha256,
            result_type="connection",
            result_id=connection_id,
            outcome="connection_draft",
            now=now,
        )
        _insert_event(
            connection,
            context=context,
            event_type="sso.connection_changed",
            subject_type="connection",
            subject_id=connection_id,
            subject_revision=1,
            outcome="connection_created",
            request_id=request_id,
            trace_id=trace_id,
            now=now,
        )
        row = _owned_connection(connection, context, connection_id)
        if row is None:
            raise ApiError("sso.unavailable")
        result = SsoConnectionResponse(
            connection=_connection_summary(connection, row),
            request_id=request_id,
            trace_id=trace_id,
        )
        connection.commit()
        return result
    except sqlite3.IntegrityError as exc:
        _rollback(connection)
        if "UNIQUE" in str(exc):
            raise ApiError("sso.connection_conflict") from None
        raise ApiError("sso.conflict") from None
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, DbError, EnterpriseSsoCryptoError, ValueError, TypeError, KeyError):
        _rollback(connection)
        raise ApiError("sso.unavailable") from None
    finally:
        connection.close()


def test_sso_connection(
    config: AppConfig,
    *,
    session_token: str,
    csrf_token: str,
    workspace_id: str,
    expected_revision: int,
    idempotency_key: str,
    payload: SsoTestRequest,
    request_id: str,
    trace_id: str,
) -> SsoTestResponse:
    request_sha256 = _request_sha256(
        {"connection_id": payload.sso_connection_id, "revision": expected_revision}
    )
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        context = _authenticate(
            connection,
            config,
            session_token=session_token,
            csrf_token=csrf_token,
            workspace_id=workspace_id,
            manage=True,
        )
        replay = _command_replay(
            connection,
            config,
            context=context,
            action="connection.test",
            idempotency_key=idempotency_key,
            request_sha256=request_sha256,
        )
        if replay is not None:
            row = _owned_connection(connection, context, replay["result_id"])
            if row is None:
                raise ApiError("sso.unavailable")
            result = SsoTestResponse(
                connection=_connection_summary(connection, row),
                idempotent_replay=True,
                request_id=request_id,
                trace_id=trace_id,
            )
            connection.commit()
            return result
        row = _owned_connection(connection, context, payload.sso_connection_id)
        if row is None:
            raise ApiError("sso.not_found")
        if row["revision"] != expected_revision or row["status"] not in {
            "draft",
            "testing",
            "active",
        }:
            raise ApiError("sso.revision_conflict")
        _validate_connection_test(connection, row, now=_utcnow())
        existing_active = connection.execute(
            """SELECT 1 FROM sso_connections
                 WHERE workspace_id=? AND verified_domain_id=? AND status='active'
                   AND sso_connection_id<>?""",
            (context.workspace_id, row["verified_domain_id"], row["sso_connection_id"]),
        ).fetchone()
        if existing_active is not None:
            raise ApiError("sso.connection_conflict")
        now = _utcnow()
        current_revision = int(row["revision"])
        current_status = row["status"]
        current_updated_at = row["updated_at"]
        if current_status != "testing":
            first_stamp = _later_stamp(current_updated_at, now)
            changed = connection.execute(
                """UPDATE sso_connections SET status='testing',revision=revision+1,
                     updated_by_account_id=?,updated_at=?
                     WHERE sso_connection_id=? AND workspace_id=? AND tenant_id=?
                       AND revision=? AND status=?""",
                (
                    context.account_id,
                    first_stamp,
                    row["sso_connection_id"],
                    context.workspace_id,
                    context.tenant_id,
                    current_revision,
                    current_status,
                ),
            ).rowcount
            if changed != 1:
                raise ApiError("sso.revision_conflict")
            current_revision += 1
            current_updated_at = first_stamp
        final_stamp = _later_stamp(current_updated_at, now + timedelta(milliseconds=1))
        changed = connection.execute(
            """UPDATE sso_connections SET status='active',revision=revision+1,tested_at=?,
                 disabled_at=NULL,updated_by_account_id=?,updated_at=?
                 WHERE sso_connection_id=? AND workspace_id=? AND tenant_id=?
                   AND revision=? AND status='testing'""",
            (
                final_stamp,
                context.account_id,
                final_stamp,
                row["sso_connection_id"],
                context.workspace_id,
                context.tenant_id,
                current_revision,
            ),
        ).rowcount
        if changed != 1:
            raise ApiError("sso.revision_conflict")
        connection.execute(
            """UPDATE sso_certificates SET status='active',activated_at=?
                 WHERE sso_connection_id=? AND status='staged'""",
            (final_stamp, row["sso_connection_id"]),
        )
        _ensure_optional_policy(connection, context, row["sso_connection_id"], now)
        final_revision = current_revision + 1
        _insert_command(
            connection,
            config,
            context=context,
            action="connection.test",
            idempotency_key=idempotency_key,
            request_sha256=request_sha256,
            result_type="connection",
            result_id=row["sso_connection_id"],
            outcome="connection_active",
            now=now,
        )
        _insert_event(
            connection,
            context=context,
            event_type="sso.connection_changed",
            subject_type="connection",
            subject_id=row["sso_connection_id"],
            subject_revision=final_revision,
            outcome="connection_tested",
            request_id=request_id,
            trace_id=trace_id,
            now=now,
        )
        updated = _owned_connection(connection, context, row["sso_connection_id"])
        if updated is None:
            raise ApiError("sso.unavailable")
        result = SsoTestResponse(
            connection=_connection_summary(connection, updated),
            request_id=request_id,
            trace_id=trace_id,
        )
        connection.commit()
        return result
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, DbError, EnterpriseSsoCryptoError, ValueError, TypeError, KeyError):
        _rollback(connection)
        raise ApiError("sso.unavailable") from None
    finally:
        connection.close()


def update_sso_policy(
    config: AppConfig,
    *,
    session_token: str,
    csrf_token: str,
    workspace_id: str,
    expected_revision: int,
    idempotency_key: str,
    payload: SsoPolicyUpdateRequest,
    request_id: str,
    trace_id: str,
) -> SsoPolicyResponse:
    request_sha256 = _request_sha256(
        {
            "mode": payload.mode,
            "active_connection_id": payload.active_connection_id,
            "break_glass_account_id": payload.break_glass_account_id,
            "revision": expected_revision,
        }
    )
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        context = _authenticate(
            connection,
            config,
            session_token=session_token,
            csrf_token=csrf_token,
            workspace_id=workspace_id,
            manage=True,
        )
        replay = _command_replay(
            connection,
            config,
            context=context,
            action="policy.update",
            idempotency_key=idempotency_key,
            request_sha256=request_sha256,
        )
        if replay is not None:
            row = connection.execute(
                "SELECT * FROM sso_policies WHERE sso_policy_id=? AND workspace_id=?",
                (replay["result_id"], context.workspace_id),
            ).fetchone()
            if row is None:
                raise ApiError("sso.unavailable")
            result = SsoPolicyResponse(
                policy=_policy_summary(row),
                idempotent_replay=True,
                request_id=request_id,
                trace_id=trace_id,
            )
            connection.commit()
            return result
        current = connection.execute(
            "SELECT * FROM sso_policies WHERE workspace_id=? AND tenant_id=?",
            (context.workspace_id, context.tenant_id),
        ).fetchone()
        current_revision = int(current["revision"]) if current is not None else 0
        if current_revision != expected_revision:
            raise ApiError("sso.revision_conflict")
        if payload.active_connection_id is not None:
            active = _owned_connection(connection, context, payload.active_connection_id)
            if active is None:
                raise ApiError("sso.not_found")
            if active["status"] != "active" or active["tested_at"] is None:
                raise ApiError("sso.policy_precondition_failed")
        if payload.mode == "enforced":
            _require_enforced_policy_preconditions(
                connection,
                context,
                payload.active_connection_id,
                payload.break_glass_account_id,
            )
        now = _utcnow()
        if current is None:
            policy_id = _id("ssp")
            connection.execute(
                """INSERT INTO sso_policies(
                     sso_policy_id,workspace_id,tenant_id,mode,active_connection_id,
                     break_glass_account_id,revision,updated_by_account_id,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,1,?,?,?)""",
                (
                    policy_id,
                    context.workspace_id,
                    context.tenant_id,
                    payload.mode,
                    payload.active_connection_id,
                    payload.break_glass_account_id,
                    context.account_id,
                    _db_time(now),
                    _db_time(now),
                ),
            )
        else:
            policy_id = current["sso_policy_id"]
            stamp = _later_stamp(current["updated_at"], now)
            changed = connection.execute(
                """UPDATE sso_policies SET mode=?,active_connection_id=?,
                     break_glass_account_id=?,revision=revision+1,updated_by_account_id=?,
                     updated_at=? WHERE sso_policy_id=? AND workspace_id=? AND tenant_id=?
                     AND revision=?""",
                (
                    payload.mode,
                    payload.active_connection_id,
                    payload.break_glass_account_id,
                    context.account_id,
                    stamp,
                    policy_id,
                    context.workspace_id,
                    context.tenant_id,
                    expected_revision,
                ),
            ).rowcount
            if changed != 1:
                raise ApiError("sso.revision_conflict")
        _insert_command(
            connection,
            config,
            context=context,
            action="policy.update",
            idempotency_key=idempotency_key,
            request_sha256=request_sha256,
            result_type="policy",
            result_id=policy_id,
            outcome="policy_updated",
            now=now,
        )
        row = connection.execute(
            "SELECT * FROM sso_policies WHERE sso_policy_id=?", (policy_id,)
        ).fetchone()
        if row is None:
            raise ApiError("sso.unavailable")
        result = SsoPolicyResponse(
            policy=_policy_summary(row), request_id=request_id, trace_id=trace_id
        )
        connection.commit()
        return result
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, DbError, EnterpriseSsoCryptoError, ValueError, TypeError, KeyError):
        _rollback(connection)
        raise ApiError("sso.unavailable") from None
    finally:
        connection.close()


def discover_login(
    config: AppConfig,
    *,
    email_or_domain: str,
    browser_token: str,
    request_id: str,
    trace_id: str,
) -> LoginDiscoveryResponse:
    domain = _discovery_domain(email_or_domain)
    now = _utcnow()
    expires_at = now + _STATE_TTL
    connection = _connect(config)
    try:
        rows = connection.execute(
            """SELECT connection.*,domain.verified_domain_id,domain.domain_normalized,
                      policy.active_connection_id
                 FROM verified_domains domain
                 JOIN sso_connections connection
                   ON connection.verified_domain_id=domain.verified_domain_id
                  AND connection.workspace_id=domain.workspace_id
                  AND connection.tenant_id=domain.tenant_id
                 LEFT JOIN sso_policies policy
                   ON policy.workspace_id=domain.workspace_id AND policy.tenant_id=domain.tenant_id
                WHERE domain.domain_hmac=? AND domain.status='verified'
                  AND connection.status='active'
                  AND (policy.active_connection_id IS NULL
                       OR policy.active_connection_id=connection.sso_connection_id)
                ORDER BY CASE WHEN policy.active_connection_id=connection.sso_connection_id
                              THEN 0 ELSE 1 END,connection.sso_connection_id
                LIMIT 2""",
            (enterprise_sso_hmac(config.enterprise_sso_hmac_secret, "domain", domain),),
        ).fetchall()
        if len(rows) != 1:
            return LoginDiscoveryResponse(
                mode="password",
                transaction_id=secrets.token_urlsafe(32),
                connection_code=None,
                authorization_url=None,
                expires_at=expires_at,
                request_id=request_id,
                trace_id=trace_id,
            )
        row = rows[0]
        state = secrets.token_urlsafe(32)
        state_id = _id("sls")
        browser_hmac = enterprise_sso_hmac(
            config.enterprise_sso_hmac_secret, "browser", browser_token
        )
        public_config = _public_config(row)
        if row["protocol"] == "oidc":
            nonce = secrets.token_urlsafe(32)
            verifier = secrets.token_urlsafe(48)
            challenge = base64.urlsafe_b64encode(
                hashlib.sha256(verifier.encode("ascii")).digest()
            ).rstrip(b"=").decode("ascii")
            verifier_ciphertext = encrypt_enterprise_sso_secret(
                verifier,
                secret=config.enterprise_sso_encryption_secret,
                owner=EnterpriseSsoSecretOwner(
                    row["workspace_id"], row["tenant_id"], state_id, "pkce_verifier"
                ),
            )
            nonce_hash = enterprise_sso_hmac(
                config.enterprise_sso_hmac_secret, "nonce", nonce
            )
            saml_request_hash = None
            authorization_url = oidc_authorization_url(
                endpoint=public_config["authorization_endpoint"],
                client_id=public_config["client_id"],
                redirect_uri=public_config["redirect_uri"],
                state=state,
                nonce=nonce,
                code_challenge=challenge,
            )
        else:
            request_correlation = "_" + uuid4().hex
            verifier_ciphertext = None
            nonce_hash = None
            saml_request_hash = enterprise_sso_hmac(
                config.enterprise_sso_hmac_secret, "saml_request", request_correlation
            )
            authorization_url = _saml_authorization_url(
                endpoint=public_config["sso_url"],
                request_id=request_correlation,
                issuer=public_config["audience"],
                destination=public_config["sso_url"],
                callback=public_config["redirect_uri"],
                state=state,
                now=now,
            )
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """INSERT INTO sso_login_states(
                 sso_login_state_id,workspace_id,tenant_id,verified_domain_id,
                 sso_connection_id,protocol,state_hash,browser_hmac,nonce_hash,
                 verifier_ciphertext,request_id_hash,status,revision,expires_at,
                 consumed_at,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,'pending',1,?,NULL,?,?)""",
            (
                state_id,
                row["workspace_id"],
                row["tenant_id"],
                row["verified_domain_id"],
                row["sso_connection_id"],
                row["protocol"],
                enterprise_sso_hmac(config.enterprise_sso_hmac_secret, "state", state),
                browser_hmac,
                nonce_hash,
                verifier_ciphertext,
                saml_request_hash,
                _db_time(expires_at),
                _db_time(now),
                _db_time(now),
            ),
        )
        connection.commit()
        return LoginDiscoveryResponse(
            mode="sso",
            transaction_id=state,
            connection_code=row["connection_code"],
            authorization_url=authorization_url,
            expires_at=expires_at,
            request_id=request_id,
            trace_id=trace_id,
        )
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, DbError, EnterpriseSsoCryptoError, ValueError, TypeError, KeyError):
        _rollback(connection)
        raise ApiError("sso.unavailable") from None
    finally:
        connection.close()


def complete_sso_callback(
    config: AppConfig,
    *,
    payload: SsoCallbackRequest,
    browser_token: str,
    request_id: str,
    trace_id: str,
    user_agent: str | None,
    client_host: str | None,
) -> FederatedSessionBundle:
    initial = _load_login_state(config, payload.state, browser_token, payload.protocol)
    try:
        identity = _validate_callback_protocol(config, initial, payload)
        return _commit_federated_login(
            config,
            initial=initial,
            identity=identity,
            browser_token=browser_token,
            request_id=request_id,
            trace_id=trace_id,
            user_agent=user_agent,
            client_host=client_host,
        )
    except ApiError as exc:
        _record_login_failure(config, initial, request_id=request_id, trace_id=trace_id)
        if exc.code in {"sso.state_invalid", "sso.unavailable"}:
            raise
        raise ApiError("sso.login_failed") from None


def _read_domain_for_external_check(
    config: AppConfig,
    *,
    session_token: str,
    csrf_token: str,
    workspace_id: str,
    domain_id: str,
    expected_revision: int,
) -> sqlite3.Row:
    connection = _connect(config)
    try:
        connection.execute("BEGIN")
        context = _authenticate(
            connection,
            config,
            session_token=session_token,
            csrf_token=csrf_token,
            workspace_id=workspace_id,
            manage=True,
        )
        row = _owned_domain(connection, context, domain_id)
        if row is None:
            raise ApiError("sso.not_found")
        if row["revision"] != expected_revision or row["status"] != "pending":
            raise ApiError("sso.revision_conflict")
        connection.commit()
        return row
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, DbError, ValueError, TypeError, KeyError):
        _rollback(connection)
        raise ApiError("sso.unavailable") from None
    finally:
        connection.close()


def _load_login_state(
    config: AppConfig, state: str, browser_token: str, protocol: str
) -> dict[str, object]:
    connection = _connect(config)
    try:
        row = connection.execute(
            """SELECT state.*,connection.issuer,connection.public_config_json,
                      connection.client_secret_ciphertext,connection.secret_version,
                      connection.status connection_status,domain.domain_normalized,
                      domain.status domain_status
                 FROM sso_login_states state
                 JOIN sso_connections connection
                   ON connection.sso_connection_id=state.sso_connection_id
                  AND connection.workspace_id=state.workspace_id
                  AND connection.tenant_id=state.tenant_id
                 JOIN verified_domains domain
                   ON domain.verified_domain_id=state.verified_domain_id
                  AND domain.workspace_id=state.workspace_id
                  AND domain.tenant_id=state.tenant_id
                WHERE state.state_hash=?""",
            (enterprise_sso_hmac(config.enterprise_sso_hmac_secret, "state", state),),
        ).fetchone()
        if (
            row is None
            or row["status"] != "pending"
            or row["protocol"] != protocol
            or row["connection_status"] != "active"
            or row["domain_status"] != "verified"
            or _parse_time(row["expires_at"]) <= _utcnow()
            or not secrets.compare_digest(
                row["browser_hmac"],
                enterprise_sso_hmac(
                    config.enterprise_sso_hmac_secret, "browser", browser_token
                ),
            )
        ):
            raise ApiError("sso.state_invalid")
        certificates = connection.execute(
            """SELECT purpose,fingerprint_sha256,public_material,status,not_before,not_after
                 FROM sso_certificates
                WHERE sso_connection_id=? AND status='active'
                ORDER BY not_after DESC,sso_certificate_id""",
            (row["sso_connection_id"],),
        ).fetchall()
        result = dict(row)
        result["certificates"] = [dict(item) for item in certificates]
        return result
    except ApiError:
        raise
    except (sqlite3.Error, DbError, EnterpriseSsoCryptoError, ValueError, TypeError, KeyError):
        raise ApiError("sso.unavailable") from None
    finally:
        connection.close()


def _validate_callback_protocol(
    config: AppConfig, initial: dict[str, object], payload: SsoCallbackRequest
) -> ProtocolIdentity:
    public_config = json.loads(str(initial["public_config_json"]))
    certificates = initial["certificates"]
    if payload.protocol == "oidc":
        verifier = decrypt_enterprise_sso_secret(
            initial["verifier_ciphertext"],
            secret=config.enterprise_sso_encryption_secret,
            owner=EnterpriseSsoSecretOwner(
                str(initial["workspace_id"]),
                str(initial["tenant_id"]),
                str(initial["sso_login_state_id"]),
                "pkce_verifier",
            ),
        )
        client_secret = decrypt_enterprise_sso_secret(
            initial["client_secret_ciphertext"],
            secret=config.enterprise_sso_encryption_secret,
            owner=EnterpriseSsoSecretOwner(
                str(initial["workspace_id"]),
                str(initial["tenant_id"]),
                str(initial["sso_connection_id"]),
                "oidc_client_secret",
            ),
        )
        id_token = exchange_oidc_code(
            token_endpoint=public_config["token_endpoint"],
            client_id=public_config["client_id"],
            client_secret=client_secret,
            redirect_uri=public_config["redirect_uri"],
            code=payload.code or "",
            verifier=verifier,
        )
        jwks = tuple(
            json.loads(item["public_material"])
            for item in certificates
            if item["purpose"] == "oidc_jwk"
        )
        identity = validate_oidc_id_token(
            id_token,
            issuer=str(initial["issuer"]),
            audience=public_config["client_id"],
            nonce=None,
            jwks=jwks,
        )
        if identity.nonce is None or not secrets.compare_digest(
            str(initial["nonce_hash"]),
            enterprise_sso_hmac(
                config.enterprise_sso_hmac_secret, "nonce", identity.nonce
            ),
        ):
            raise ApiError("sso.login_failed")
        return identity
    saml_request = saml_response_request_id(payload.saml_response or "")
    if not secrets.compare_digest(
        str(initial["request_id_hash"]),
        enterprise_sso_hmac(
            config.enterprise_sso_hmac_secret, "saml_request", saml_request
        ),
    ):
        raise ApiError("sso.login_failed")
    saml_certificates = [
        item for item in certificates if item["purpose"] == "saml_signing"
    ]
    if len(saml_certificates) != 1:
        raise ApiError("sso.login_failed")
    return validate_saml_response(
        payload.saml_response or "",
        certificate_pem=saml_certificates[0]["public_material"],
        issuer=str(initial["issuer"]),
        audience=public_config["audience"],
        destination=public_config["redirect_uri"],
        request_id=saml_request,
        now=_utcnow(),
    )


def _commit_federated_login(
    config: AppConfig,
    *,
    initial: dict[str, object],
    identity: ProtocolIdentity,
    browser_token: str,
    request_id: str,
    trace_id: str,
    user_agent: str | None,
    client_host: str | None,
) -> FederatedSessionBundle:
    normalized_email = normalize_global_email(identity.email)
    if (
        normalized_email is None
        or normalized_email.rsplit("@", 1)[1] != initial["domain_normalized"]
    ):
        raise ApiError("sso.login_failed")
    subject_hmac = enterprise_sso_hmac(
        config.enterprise_sso_hmac_secret,
        "external_subject",
        f"{initial['sso_connection_id']}\x1f{identity.subject}",
    )
    email_hmac = enterprise_sso_hmac(
        config.enterprise_sso_hmac_secret, "verified_email", normalized_email
    )
    assertion_hmac = enterprise_sso_hmac(
        config.enterprise_sso_hmac_secret,
        "assertion",
        f"{initial['sso_connection_id']}\x1f{identity.assertion_key}",
    )
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        state = connection.execute(
            """SELECT * FROM sso_login_states
                 WHERE sso_login_state_id=? AND state_hash=? AND status='pending'""",
            (
                initial["sso_login_state_id"],
                initial["state_hash"],
            ),
        ).fetchone()
        if (
            state is None
            or _parse_time(state["expires_at"]) <= _utcnow()
            or not secrets.compare_digest(
                state["browser_hmac"],
                enterprise_sso_hmac(
                    config.enterprise_sso_hmac_secret, "browser", browser_token
                ),
            )
        ):
            raise ApiError("sso.state_invalid")
        if connection.execute(
            "SELECT 1 FROM sso_replay_guards WHERE assertion_hmac=?", (assertion_hmac,)
        ).fetchone() is not None:
            raise ApiError("sso.login_failed")
        link = connection.execute(
            """SELECT link.account_id,link.membership_id
                 FROM account_federation_links link
                 JOIN workspace_memberships member
                   ON member.workspace_membership_id=link.membership_id
                  AND member.workspace_id=link.workspace_id
                  AND member.tenant_id=link.tenant_id
                  AND member.account_id=link.account_id
                WHERE link.sso_connection_id=? AND link.external_subject_hmac=?
                  AND link.verified_email_hmac=? AND link.status='active'
                  AND member.status='active' AND member.role_code!='service_account'""",
            (initial["sso_connection_id"], subject_hmac, email_hmac),
        ).fetchone()
        if link is None:
            candidates = connection.execute(
                """SELECT account.account_id,member.workspace_membership_id membership_id
                     FROM global_accounts account
                     JOIN workspace_memberships member ON member.account_id=account.account_id
                    WHERE account.email_normalized=? AND account.status='active'
                      AND account.login_enabled=1 AND member.workspace_id=?
                      AND member.tenant_id=? AND member.status='active'
                      AND member.role_code!='service_account'""",
                (
                    normalized_email,
                    initial["workspace_id"],
                    initial["tenant_id"],
                ),
            ).fetchall()
            if len(candidates) != 1:
                raise ApiError("sso.login_failed")
            link = candidates[0]
            now = _utcnow()
            connection.execute(
                """INSERT INTO account_federation_links(
                     account_federation_link_id,workspace_id,tenant_id,sso_connection_id,
                     account_id,membership_id,external_subject_hmac,verified_email_hmac,
                     status,revision,linked_at,disabled_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,'active',1,?,NULL,?)""",
                (
                    _id("afl"),
                    initial["workspace_id"],
                    initial["tenant_id"],
                    initial["sso_connection_id"],
                    link["account_id"],
                    link["membership_id"],
                    subject_hmac,
                    email_hmac,
                    _db_time(now),
                    _db_time(now),
                ),
            )
        now = _utcnow()
        replay_expires = min(identity.expires_at, now + timedelta(hours=1))
        if replay_expires <= now:
            raise ApiError("sso.login_failed")
        connection.execute(
            "INSERT INTO sso_replay_guards VALUES(?,?,?,?,?,?,?,?)",
            (
                _id("srg"),
                initial["workspace_id"],
                initial["tenant_id"],
                initial["sso_connection_id"],
                initial["protocol"],
                assertion_hmac,
                _db_time(replay_expires),
                _db_time(now),
            ),
        )
        global_issued = issue_global_session_in_transaction(
            connection,
            config,
            account_id=link["account_id"],
            now=now,
            user_agent=user_agent,
            client_host=client_host,
        )
        workspace_token, workspace_csrf, workspace_session_id = (
            issue_federated_workspace_session_in_transaction(
            connection,
            config,
            global_session_id=global_issued.current_session.session_id,
            global_expires_at=global_issued.current_session.expires_at,
            account_id=link["account_id"],
            workspace_id=str(initial["workspace_id"]),
            sso_connection_id=str(initial["sso_connection_id"]),
            now=now,
            request_id=request_id,
            trace_id=trace_id,
            )
        )
        grant_expires = min(
            identity.expires_at,
            global_issued.current_session.expires_at,
            now + timedelta(seconds=config.workspace_session_ttl_seconds),
        )
        connection.execute(
            """INSERT INTO enterprise_identity_session_grants(
                 enterprise_identity_session_grant_id,global_session_id,
                 workspace_session_id,workspace_id,tenant_id,account_id,grant_type,
                 sso_connection_id,reason_code,status,revision,expires_at,consumed_at,
                 created_at,updated_at)
               VALUES(?,?,?,?,?,?,'federated',?,NULL,'consumed',1,?,?,?,?)""",
            (
                _id("esg"),
                global_issued.current_session.session_id,
                workspace_session_id,
                initial["workspace_id"],
                initial["tenant_id"],
                link["account_id"],
                initial["sso_connection_id"],
                _db_time(grant_expires),
                _db_time(now),
                _db_time(now),
                _db_time(now),
            ),
        )
        stamp = _later_stamp(state["updated_at"], now)
        changed = connection.execute(
            """UPDATE sso_login_states SET status='consumed',revision=2,consumed_at=?,updated_at=?
                 WHERE sso_login_state_id=? AND status='pending' AND revision=1""",
            (stamp, stamp, state["sso_login_state_id"]),
        ).rowcount
        if changed != 1:
            raise ApiError("sso.state_invalid")
        connection.commit()
        return FederatedSessionBundle(
            response=SsoCallbackResponse(
                workspace_id=str(initial["workspace_id"]),
                request_id=request_id,
                trace_id=trace_id,
            ),
            global_session_token=global_issued.session_token,
            global_csrf_token=global_issued.csrf_token,
            workspace_session_token=workspace_token,
            workspace_csrf_token=workspace_csrf,
        )
    except sqlite3.IntegrityError:
        _rollback(connection)
        raise ApiError("sso.login_failed") from None
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, DbError, EnterpriseSsoCryptoError, ValueError, TypeError, KeyError):
        _rollback(connection)
        raise ApiError("sso.unavailable") from None
    finally:
        connection.close()


def _authenticate(
    connection: sqlite3.Connection,
    config: AppConfig,
    *,
    session_token: str,
    csrf_token: str | None,
    workspace_id: str,
    manage: bool,
) -> WorkspaceSessionContext:
    context = authenticate_workspace_session_in_transaction(
        connection,
        config,
        session_token=session_token,
        csrf_token=csrf_token,
        touch=False,
    )
    if context.workspace_id != workspace_id:
        raise ApiError("sso.not_found")
    allowed_roles = _MANAGE_ROLES if manage else _READ_ROLES
    permission = "enterprise_sso.manage" if manage else "enterprise_sso.read"
    if context.membership_role not in allowed_roles or permission not in context.permission_codes:
        raise ApiError("workspace_session.permission_denied")
    return context


def _connection_material(
    config: AppConfig, payload: SsoConnectionCreateRequest
) -> dict[str, object]:
    redirect_uri = config.control_api_base_url.rstrip("/") + "/identity/sso/callback"
    if payload.protocol == "oidc":
        authorization_endpoint = validate_fixed_https_endpoint(payload.authorization_endpoint)
        token_endpoint = validate_fixed_https_endpoint(payload.token_endpoint)
        keys = validate_oidc_jwks(payload.jwks)
        certificates = []
        now = _utcnow()
        for key in keys:
            material = _canonical_json(key)
            certificates.append(
                {
                    "purpose": "oidc_jwk",
                    "fingerprint": hashlib.sha256(material.encode("utf-8")).hexdigest(),
                    "material": material,
                    "not_before": now - timedelta(minutes=1),
                    "not_after": now + timedelta(days=365),
                }
            )
        return {
            "public_config": {
                "authorization_endpoint": authorization_endpoint,
                "token_endpoint": token_endpoint,
                "client_id": payload.client_id,
                "redirect_uri": redirect_uri,
            },
            "certificates": certificates,
            "fingerprints": [item["fingerprint"] for item in certificates],
        }
    sso_url = validate_fixed_https_endpoint(payload.sso_url)
    certificate = parse_saml_certificate(payload.certificate_pem)
    return {
        "public_config": {
            "sso_url": sso_url,
            "audience": config.control_api_base_url.rstrip("/") + "/identity/sso/metadata",
            "redirect_uri": redirect_uri,
        },
        "certificates": [
            {
                "purpose": "saml_signing",
                "fingerprint": certificate.fingerprint_sha256,
                "material": certificate.public_pem,
                "not_before": certificate.not_before,
                "not_after": certificate.not_after,
            }
        ],
        "fingerprints": [certificate.fingerprint_sha256],
    }


def _validate_connection_test(
    connection: sqlite3.Connection, row: sqlite3.Row, *, now: datetime
) -> None:
    public_config = _public_config(row)
    if row["protocol"] == "oidc":
        validate_fixed_https_endpoint(public_config.get("authorization_endpoint"))
        validate_fixed_https_endpoint(public_config.get("token_endpoint"))
        if type(public_config.get("client_id")) is not str:
            raise ApiError("sso.connection_test_failed")
    else:
        validate_fixed_https_endpoint(public_config.get("sso_url"))
    certificates = connection.execute(
        """SELECT * FROM sso_certificates
             WHERE sso_connection_id=? AND status IN('staged','active')""",
        (row["sso_connection_id"],),
    ).fetchall()
    if not certificates or any(
        _parse_time(item["not_before"]) > now
        or _parse_time(item["not_after"]) <= now + _CERTIFICATE_MIN_REMAINING
        for item in certificates
    ):
        raise ApiError("sso.connection_test_failed")


def _require_enforced_policy_preconditions(
    connection: sqlite3.Connection,
    context: WorkspaceSessionContext,
    connection_id: str | None,
    break_glass_account_id: str | None,
) -> None:
    if connection_id is None or break_glass_account_id is None:
        raise ApiError("sso.policy_precondition_failed")
    now = _utcnow()
    ready = connection.execute(
        """SELECT 1 FROM sso_connections connection
             WHERE connection.sso_connection_id=? AND connection.workspace_id=?
               AND connection.tenant_id=? AND connection.status='active'
               AND connection.tested_at IS NOT NULL
               AND EXISTS(SELECT 1 FROM sso_certificates certificate
                 WHERE certificate.sso_connection_id=connection.sso_connection_id
                   AND certificate.status='active' AND certificate.not_after>?)""",
        (
            connection_id,
            context.workspace_id,
            context.tenant_id,
            _db_time(now + _CERTIFICATE_MIN_REMAINING),
        ),
    ).fetchone()
    owners = connection.execute(
        """SELECT COUNT(*) FROM workspace_memberships
             WHERE workspace_id=? AND tenant_id=? AND role_code='owner' AND status='active'""",
        (context.workspace_id, context.tenant_id),
    ).fetchone()[0]
    break_glass = connection.execute(
        """SELECT 1 FROM workspace_memberships member
             JOIN global_accounts account ON account.account_id=member.account_id
             JOIN account_credentials credential ON credential.account_id=account.account_id
               AND credential.credential_type='password' AND credential.status='active'
             JOIN account_mfa_factors factor ON factor.account_id=account.account_id
               AND factor.status='active'
            WHERE member.workspace_id=? AND member.tenant_id=? AND member.account_id=?
              AND member.role_code='owner' AND member.status='active'
              AND account.status='active' AND account.login_enabled=1""",
        (context.workspace_id, context.tenant_id, break_glass_account_id),
    ).fetchone()
    if ready is None or owners < 1 or break_glass is None:
        raise ApiError("sso.policy_precondition_failed")


def _ensure_optional_policy(
    connection: sqlite3.Connection,
    context: WorkspaceSessionContext,
    connection_id: str,
    now: datetime,
) -> None:
    current = connection.execute(
        "SELECT * FROM sso_policies WHERE workspace_id=? AND tenant_id=?",
        (context.workspace_id, context.tenant_id),
    ).fetchone()
    if current is None:
        connection.execute(
            """INSERT INTO sso_policies(
                 sso_policy_id,workspace_id,tenant_id,mode,active_connection_id,
                 break_glass_account_id,revision,updated_by_account_id,created_at,updated_at)
               VALUES(?,?,?,'optional',?,NULL,1,?,?,?)""",
            (
                _id("ssp"),
                context.workspace_id,
                context.tenant_id,
                connection_id,
                context.account_id,
                _db_time(now),
                _db_time(now),
            ),
        )
    elif current["mode"] == "optional" and current["active_connection_id"] is None:
        stamp = _later_stamp(current["updated_at"], now)
        connection.execute(
            """UPDATE sso_policies SET active_connection_id=?,revision=revision+1,
                 updated_by_account_id=?,updated_at=? WHERE sso_policy_id=?""",
            (connection_id, context.account_id, stamp, current["sso_policy_id"]),
        )


def _record_login_failure(
    config: AppConfig,
    initial: dict[str, object],
    *,
    request_id: str,
    trace_id: str,
) -> None:
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        now = _utcnow()
        payload_json = _canonical_json(
            {"outcome": "assertion_rejected", "schema_version": 1}
        )
        connection.execute(
            """INSERT INTO enterprise_identity_event_outbox(
                 enterprise_identity_event_id,workspace_id,tenant_id,actor_account_id,
                 event_type,subject_type,subject_id,subject_revision,request_id,trace_id,
                 payload_json,payload_sha256,delivery_status,attempt_count,next_attempt_at,
                 delivered_at,occurred_at,updated_at)
               VALUES(?,?,?,NULL,'sso.login_failed','connection',?,1,?,?,?,?,'pending',0,
                 ?,NULL,?,?)""",
            (
                _id("eie"),
                initial["workspace_id"],
                initial["tenant_id"],
                initial["sso_connection_id"],
                request_id,
                trace_id,
                payload_json,
                hashlib.sha256(payload_json.encode("utf-8")).hexdigest(),
                _db_time(now),
                _db_time(now),
                _db_time(now),
            ),
        )
        connection.commit()
    except Exception:
        _rollback(connection)
    finally:
        connection.close()


def _lookup_txt_records(domain: str) -> frozenset[str]:
    try:
        with httpx.Client(timeout=3.0, follow_redirects=False, trust_env=False) as client:
            response = client.get(
                "https://cloudflare-dns.com/dns-query",
                params={"name": domain, "type": "TXT"},
                headers={"Accept": "application/dns-json"},
            )
        if response.status_code != 200 or len(response.content) > 65_536:
            raise ApiError("sso.domain_verification_failed")
        payload = response.json()
        answers = payload.get("Answer", []) if type(payload) is dict else []
        if type(answers) is not list or len(answers) > 64:
            raise ApiError("sso.domain_verification_failed")
        values: set[str] = set()
        for answer in answers:
            if type(answer) is not dict or answer.get("type") != 16:
                continue
            data = answer.get("data")
            if type(data) is str and len(data) <= 1024:
                values.add(data.strip().strip('"').replace('" "', ""))
        return frozenset(values)
    except ApiError:
        raise
    except (httpx.HTTPError, json.JSONDecodeError, ValueError, TypeError):
        raise ApiError("sso.domain_verification_failed") from None


def _saml_authorization_url(
    *,
    endpoint: str,
    request_id: str,
    issuer: str,
    destination: str,
    callback: str,
    state: str,
    now: datetime,
) -> str:
    protocol_namespace = "urn:oasis:names:tc:SAML:2.0:protocol"
    assertion_namespace = "urn:oasis:names:tc:SAML:2.0:assertion"
    authn_request = etree.Element(
        f"{{{protocol_namespace}}}AuthnRequest",
        nsmap={"samlp": protocol_namespace, "saml": assertion_namespace},
        ID=request_id,
        Version="2.0",
        IssueInstant=_db_time(now),
        Destination=destination,
        AssertionConsumerServiceURL=callback,
    )
    issuer_element = etree.SubElement(authn_request, f"{{{assertion_namespace}}}Issuer")
    issuer_element.text = issuer
    xml = etree.tostring(authn_request, encoding="UTF-8", xml_declaration=False)
    compressor = zlib.compressobj(wbits=-15)
    encoded = base64.b64encode(compressor.compress(xml) + compressor.flush()).decode("ascii")
    return endpoint + ("&" if "?" in endpoint else "?") + urlencode(
        {"SAMLRequest": encoded, "RelayState": state}
    )


def _command_replay(
    connection: sqlite3.Connection,
    config: AppConfig,
    *,
    context: WorkspaceSessionContext,
    action: str,
    idempotency_key: str,
    request_sha256: str,
) -> sqlite3.Row | None:
    row = connection.execute(
        """SELECT * FROM enterprise_identity_commands
             WHERE workspace_id=? AND actor_account_id=? AND action=? AND idempotency_hmac=?""",
        (
            context.workspace_id,
            context.account_id,
            action,
            enterprise_sso_hmac(
                config.enterprise_sso_hmac_secret,
                "idempotency",
                f"{context.workspace_id}\x1f{context.account_id}\x1f{action}\x1f{idempotency_key}",
            ),
        ),
    ).fetchone()
    if row is not None and not secrets.compare_digest(row["request_sha256"], request_sha256):
        raise ApiError("sso.idempotency_conflict")
    return row


def _insert_command(
    connection: sqlite3.Connection,
    config: AppConfig,
    *,
    context: WorkspaceSessionContext,
    action: str,
    idempotency_key: str,
    request_sha256: str,
    result_type: str,
    result_id: str,
    outcome: str,
    now: datetime,
) -> None:
    connection.execute(
        """INSERT INTO enterprise_identity_commands(
             enterprise_identity_command_id,workspace_id,tenant_id,actor_account_id,
             action,idempotency_hmac,request_sha256,result_type,result_id,status,
             safe_outcome_code,created_at)
           VALUES(?,?,?,?,?,?,?,?,?,'succeeded',?,?)""",
        (
            _id("eic"),
            context.workspace_id,
            context.tenant_id,
            context.account_id,
            action,
            enterprise_sso_hmac(
                config.enterprise_sso_hmac_secret,
                "idempotency",
                f"{context.workspace_id}\x1f{context.account_id}\x1f{action}\x1f{idempotency_key}",
            ),
            request_sha256,
            result_type,
            result_id,
            outcome,
            _db_time(now),
        ),
    )


def _insert_event(
    connection: sqlite3.Connection,
    *,
    context: WorkspaceSessionContext,
    event_type: str,
    subject_type: str,
    subject_id: str,
    subject_revision: int,
    outcome: str,
    request_id: str,
    trace_id: str,
    now: datetime,
) -> None:
    payload_json = _canonical_json({"outcome": outcome, "schema_version": 1})
    connection.execute(
        """INSERT INTO enterprise_identity_event_outbox(
             enterprise_identity_event_id,workspace_id,tenant_id,actor_account_id,
             event_type,subject_type,subject_id,subject_revision,request_id,trace_id,
             payload_json,payload_sha256,delivery_status,attempt_count,next_attempt_at,
             delivered_at,occurred_at,updated_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,'pending',0,?,NULL,?,?)""",
        (
            _id("eie"),
            context.workspace_id,
            context.tenant_id,
            context.account_id,
            event_type,
            subject_type,
            subject_id,
            subject_revision,
            request_id,
            trace_id,
            payload_json,
            hashlib.sha256(payload_json.encode("utf-8")).hexdigest(),
            _db_time(now),
            _db_time(now),
            _db_time(now),
        ),
    )


def _owned_domain(
    connection: sqlite3.Connection,
    context: WorkspaceSessionContext,
    domain_id: str,
) -> sqlite3.Row | None:
    return connection.execute(
        """SELECT * FROM verified_domains
             WHERE verified_domain_id=? AND workspace_id=? AND tenant_id=?""",
        (domain_id, context.workspace_id, context.tenant_id),
    ).fetchone()


def _owned_connection(
    connection: sqlite3.Connection,
    context: WorkspaceSessionContext,
    connection_id: str,
) -> sqlite3.Row | None:
    return connection.execute(
        """SELECT * FROM sso_connections
             WHERE sso_connection_id=? AND workspace_id=? AND tenant_id=?""",
        (connection_id, context.workspace_id, context.tenant_id),
    ).fetchone()


def _domain_summary(row: sqlite3.Row) -> VerifiedDomainSummary:
    return VerifiedDomainSummary(
        verified_domain_id=row["verified_domain_id"],
        domain=row["domain_normalized"],
        status=row["status"],
        revision=row["revision"],
        challenge_expires_at=_parse_time(row["challenge_expires_at"]),
        verified_at=_optional_time(row["verified_at"]),
        updated_at=_parse_time(row["updated_at"]),
    )


def _connection_summary(
    connection: sqlite3.Connection, row: sqlite3.Row
) -> SsoConnectionSummary:
    certificates = tuple(
        SsoCertificateSummary(
            sso_certificate_id=item["sso_certificate_id"],
            purpose=item["purpose"],
            fingerprint_sha256=item["fingerprint_sha256"],
            status=item["status"],
            not_before=_parse_time(item["not_before"]),
            not_after=_parse_time(item["not_after"]),
        )
        for item in connection.execute(
            """SELECT * FROM sso_certificates WHERE sso_connection_id=?
                 ORDER BY not_after DESC,sso_certificate_id""",
            (row["sso_connection_id"],),
        )
    )
    return SsoConnectionSummary(
        sso_connection_id=row["sso_connection_id"],
        verified_domain_id=row["verified_domain_id"],
        connection_code=row["connection_code"],
        display_name=row["display_name"],
        protocol=row["protocol"],
        issuer=row["issuer"],
        status=row["status"],
        revision=row["revision"],
        tested_at=_optional_time(row["tested_at"]),
        updated_at=_parse_time(row["updated_at"]),
        certificates=certificates,
    )


def _policy_summary(row: sqlite3.Row) -> SsoPolicySummary:
    return SsoPolicySummary(
        mode=row["mode"],
        active_connection_id=row["active_connection_id"],
        break_glass_configured=row["break_glass_account_id"] is not None,
        revision=row["revision"],
        updated_at=_parse_time(row["updated_at"]),
    )


def _public_config(row: sqlite3.Row) -> dict[str, object]:
    try:
        value = json.loads(row["public_config_json"])
    except (json.JSONDecodeError, TypeError):
        raise ApiError("sso.unavailable") from None
    if type(value) is not dict:
        raise ApiError("sso.unavailable")
    return value


def _discovery_domain(value: str) -> str:
    candidate = value.rsplit("@", 1)[1] if value.count("@") == 1 else value
    return normalize_enterprise_domain(candidate)


def _domain_verification_record(config: AppConfig, domain_id: str) -> str:
    digest = enterprise_sso_hmac(
        config.enterprise_sso_hmac_secret, "domain_challenge", domain_id
    )
    return f"metahub-domain-verification={digest}"


def _request_sha256(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _connect(config: AppConfig) -> sqlite3.Connection:
    try:
        return connect_database(config.database_url)
    except DbError:
        raise ApiError("sso.unavailable") from None


def _rollback(connection: sqlite3.Connection) -> None:
    if connection.in_transaction:
        connection.rollback()


def _later_stamp(previous: str, now: datetime) -> str:
    prior = _parse_time(previous)
    return _db_time(max(now, prior + timedelta(milliseconds=1)))


def _optional_time(value: str | None) -> datetime | None:
    return _parse_time(value) if value is not None else None


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _db_time(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


__all__ = [
    "FederatedSessionBundle",
    "complete_sso_callback",
    "create_sso_connection",
    "create_verified_domain",
    "discover_login",
    "get_enterprise_identity_summary",
    "test_sso_connection",
    "update_sso_policy",
    "verify_domain",
]
