"""PRD-116 MFA, device, recovery-code, and one-shot step-up authority."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit
from uuid import uuid4

from metahub_shared.db.connection import connect_database
from metahub_shared.db.errors import DbError
from metahub_shared.db.security import hash_password_argon2id, verify_password
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError
from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers.cose import COSEAlgorithmIdentifier
from webauthn.helpers.exceptions import WebAuthnException
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from metahub_control.workspace_access.service import (
    WorkspaceSessionContext,
    authenticate_workspace_session_in_transaction,
)

from .delivery import identity_challenge_delivery
from .mfa_service import consume_active_totp_in_transaction
from .models import (
    GlobalSessionSummary,
    MfaFactorSummary,
    RecoveryChallengeSummary,
    RecoveryCodeSetSummary,
    TrustedDeviceSummary,
)
from .service import (
    authenticate_global_session_in_transaction,
    issue_global_session_in_transaction,
)

_ID_RE = re.compile(r"^[a-z]{3}_[0-9a-f]{32}$")
_ACTION_RE = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$")
_RESOURCE_TYPE_RE = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*$")
_RECOVERY_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
_WEBAUTHN_ALGORITHMS = [
    COSEAlgorithmIdentifier.ECDSA_SHA_256,
    COSEAlgorithmIdentifier.RSASSA_PKCS1_v1_5_SHA_256,
]


@dataclass(frozen=True, slots=True, repr=False)
class PendingWebAuthn:
    factor_id: str
    challenge_id: str
    public_key_options: dict[str, object]
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class CompletedWebAuthn:
    factor_id: str
    revision: int
    verified_at: datetime


@dataclass(frozen=True, slots=True, repr=False)
class GeneratedRecoveryCodes:
    summary: RecoveryCodeSetSummary
    codes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PendingAccountRecovery:
    challenge_id: str
    ready_at: datetime
    expires_at: datetime


@dataclass(frozen=True, slots=True, repr=False)
class CompletedAccountRecovery:
    session_token: str
    csrf_token: str
    completed_at: datetime


@dataclass(frozen=True, slots=True, repr=False)
class IssuedStepUpGrant:
    grant: str
    grant_id: str
    action_code: str
    resource_type: str
    resource_id: str
    auth_method: str
    assurance_level: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class IdentitySecurityOverview:
    factors: tuple[MfaFactorSummary, ...]
    recovery_code_set: RecoveryCodeSetSummary | None
    recovery_challenge: RecoveryChallengeSummary | None
    trusted_devices: tuple[TrustedDeviceSummary, ...]
    sessions: tuple[GlobalSessionSummary, ...]


def begin_webauthn_registration(
    config: AppConfig,
    *,
    session_token: str,
    csrf_token: str,
    current_password: str,
    origin: str,
    browser_binding: str,
    request_id: str,
    trace_id: str,
) -> PendingWebAuthn:
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        identity, now = authenticate_global_session_in_transaction(
            connection,
            config,
            session_token=session_token,
            csrf_token=csrf_token,
        )
        _verify_current_password(connection, identity.account_id, current_password)
        if (
            identity.current_session.assurance_level != "mfa"
            and connection.execute(
                """SELECT 1 FROM account_mfa_factors
                     WHERE account_id=? AND status='active' LIMIT 1""",
                (identity.account_id,),
            ).fetchone()
            is not None
        ):
            raise ApiError("identity.mfa_required")
        rp_id = _webauthn_rp_id(config, origin)
        challenge = secrets.token_bytes(32)
        expires_at = now + timedelta(minutes=5)
        connection.execute(
            """UPDATE account_mfa_factors
                  SET status='disabled',disabled_at=?,updated_at=?
                WHERE account_id=? AND factor_type='webauthn' AND status='pending'""",
            (_db_time(now), _db_time(now), identity.account_id),
        )
        factor_id = _id("mfa")
        connection.execute(
            """INSERT INTO account_mfa_factors(
                 factor_id,account_id,factor_type,secret_ciphertext,secret_version,status,
                 revision,last_accepted_counter,verified_at,disabled_at,created_at,updated_at)
               VALUES(?,?,'webauthn',NULL,NULL,'pending',1,NULL,NULL,NULL,?,?)""",
            (factor_id, identity.account_id, _db_time(now), _db_time(now)),
        )
        challenge_id = _insert_challenge(
            connection,
            config,
            account_id=identity.account_id,
            global_session_id=identity.session_id,
            purpose="webauthn_registration",
            challenge=challenge,
            browser_binding=browser_binding,
            rp_id=rp_id,
            origin=origin,
            now=now,
            expires_at=expires_at,
        )
        options = generate_registration_options(
            rp_id=rp_id,
            rp_name="MetaHub",
            user_name=identity.account.email,
            user_id=identity.account_id.encode("ascii"),
            user_display_name=identity.account.display_name,
            challenge=challenge,
            timeout=300_000,
            authenticator_selection=AuthenticatorSelectionCriteria(
                resident_key=ResidentKeyRequirement.REQUIRED,
                require_resident_key=True,
                user_verification=UserVerificationRequirement.REQUIRED
            ),
            supported_pub_key_algs=_WEBAUTHN_ALGORITHMS,
        )
        _write_security_event(
            connection,
            account_id=identity.account_id,
            actor_account_id=identity.account_id,
            event_type="identity.mfa_changed",
            subject_type="factor",
            subject_id=factor_id,
            reason_code="webauthn_enrollment_started",
            request_id=request_id,
            trace_id=trace_id,
            payload={"schema_version": 1, "factor_type": "webauthn", "status": "pending"},
            now=now,
        )
        connection.commit()
        return PendingWebAuthn(
            factor_id=factor_id,
            challenge_id=challenge_id,
            public_key_options=json.loads(options_to_json(options)),
            expires_at=expires_at,
        )
    except ApiError:
        _rollback(connection)
        raise
    except (DbError, sqlite3.Error, WebAuthnException, ValueError, TypeError, KeyError):
        _rollback(connection)
        raise ApiError("identity.mfa_unavailable") from None
    finally:
        connection.close()


def complete_webauthn_registration(
    config: AppConfig,
    *,
    session_token: str,
    csrf_token: str,
    challenge_id: str,
    credential: dict[str, object],
    browser_binding: str,
    request_id: str,
    trace_id: str,
) -> CompletedWebAuthn:
    _require_id(challenge_id, "mfc")
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        identity, now = authenticate_global_session_in_transaction(
            connection,
            config,
            session_token=session_token,
            csrf_token=csrf_token,
        )
        challenge = _pending_challenge(
            connection,
            config,
            challenge_id=challenge_id,
            account_id=identity.account_id,
            global_session_id=identity.session_id,
            purpose="webauthn_registration",
            browser_binding=browser_binding,
            credential=credential,
            now=now,
        )
        _validate_extensions(credential, allowed={"credProps"})
        raw_challenge = _credential_challenge(credential)
        verified = verify_registration_response(
            credential=credential,
            expected_challenge=raw_challenge,
            expected_rp_id=str(challenge["rp_id"]),
            expected_origin=str(challenge["origin"]),
            require_user_presence=True,
            require_user_verification=True,
            supported_pub_key_algs=_WEBAUTHN_ALGORITHMS,
        )
        credential_hmac = _secret_hmac(config, "webauthn-credential", verified.credential_id)
        if connection.execute(
            "SELECT 1 FROM webauthn_credentials WHERE credential_id_hmac=?",
            (credential_hmac,),
        ).fetchone() is not None:
            raise ApiError("identity.mfa_verification_failed")
        factor = connection.execute(
            """SELECT * FROM account_mfa_factors
                 WHERE account_id=? AND factor_type='webauthn' AND status='pending'
                 ORDER BY created_at DESC LIMIT 1""",
            (identity.account_id,),
        ).fetchone()
        if factor is None:
            raise ApiError("identity.mfa_verification_failed")
        transports = _credential_transports(credential)
        connection.execute(
            """INSERT INTO webauthn_credentials(
                 webauthn_credential_id,account_id,factor_id,credential_id_hmac,
                 public_key_cose_b64,aaguid,sign_count,transports_json,status,revision,
                 last_used_at,revoked_at,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?, 'active',1,NULL,NULL,?,?)""",
            (
                _id("wac"),
                identity.account_id,
                factor["factor_id"],
                credential_hmac,
                _b64url(verified.credential_public_key),
                verified.aaguid or None,
                verified.sign_count,
                json.dumps(transports, ensure_ascii=True, separators=(",", ":")),
                _db_time(now),
                _db_time(now),
            ),
        )
        updated = connection.execute(
            """UPDATE account_mfa_factors
                  SET status='active',revision=2,verified_at=?,updated_at=?
                WHERE factor_id=? AND account_id=? AND status='pending' AND revision=1""",
            (_db_time(now), _db_time(now), factor["factor_id"], identity.account_id),
        ).rowcount
        if updated != 1:
            raise ApiError("identity.mfa_verification_failed")
        _consume_challenge(connection, challenge_id, now)
        _raise_session_assurance(
            connection,
            identity.session_id,
            identity.account_id,
            method="webauthn",
            assurance="mfa",
            now=now,
        )
        _write_security_event(
            connection,
            account_id=identity.account_id,
            actor_account_id=identity.account_id,
            event_type="identity.mfa_changed",
            subject_type="factor",
            subject_id=str(factor["factor_id"]),
            reason_code="webauthn_activated",
            request_id=request_id,
            trace_id=trace_id,
            payload={"schema_version": 1, "factor_type": "webauthn", "status": "active"},
            now=now,
        )
        connection.commit()
        return CompletedWebAuthn(
            factor_id=str(factor["factor_id"]), revision=2, verified_at=now
        )
    except ApiError:
        _rollback(connection)
        raise
    except (DbError, sqlite3.Error, WebAuthnException, ValueError, TypeError, KeyError):
        _rollback(connection)
        raise ApiError("identity.mfa_verification_failed") from None
    finally:
        connection.close()


def generate_recovery_codes(
    config: AppConfig,
    *,
    session_token: str,
    csrf_token: str,
    current_password: str,
    request_id: str,
    trace_id: str,
) -> GeneratedRecoveryCodes:
    connection = _connect(config)
    codes: tuple[str, ...] = ()
    try:
        connection.execute("BEGIN IMMEDIATE")
        identity, now = authenticate_global_session_in_transaction(
            connection,
            config,
            session_token=session_token,
            csrf_token=csrf_token,
        )
        _verify_current_password(connection, identity.account_id, current_password)
        if identity.current_session.assurance_level != "mfa":
            raise ApiError("identity.mfa_required")
        codes = tuple(_recovery_code() for _ in range(10))
        stamp = _db_time(now)
        connection.execute(
            """UPDATE recovery_code_sets
                  SET status='superseded',revision=revision+1,invalidated_at=?,updated_at=?
                WHERE account_id=? AND status='active'""",
            (stamp, stamp, identity.account_id),
        )
        connection.execute(
            """UPDATE recovery_codes SET status='revoked',updated_at=?
                 WHERE account_id=? AND status='active'""",
            (stamp, identity.account_id),
        )
        set_id = _id("rcs")
        connection.execute(
            """INSERT INTO recovery_code_sets(
                 recovery_code_set_id,account_id,status,revision,generated_count,
                 consumed_count,generated_at,invalidated_at,updated_at)
               VALUES(?,?,'active',1,10,0,?,NULL,?)""",
            (set_id, identity.account_id, stamp, stamp),
        )
        for code in codes:
            prefix = _secret_hmac(config, "recovery-code", code.encode("ascii"))
            code_hash = prefix + "$" + hash_password_argon2id(code)
            connection.execute(
                """INSERT INTO recovery_codes(
                     recovery_code_id,recovery_code_set_id,account_id,code_hash,status,
                     consumed_at,created_at,updated_at)
                   VALUES(?,?,?,?,'active',NULL,?,?)""",
                (_id("rcd"), set_id, identity.account_id, code_hash, stamp, stamp),
            )
        _write_security_event(
            connection,
            account_id=identity.account_id,
            actor_account_id=identity.account_id,
            event_type="identity.mfa_changed",
            subject_type="factor",
            subject_id=set_id,
            reason_code="recovery_codes_generated",
            request_id=request_id,
            trace_id=trace_id,
            payload={"schema_version": 1, "generated_count": 10},
            now=now,
        )
        connection.commit()
        return GeneratedRecoveryCodes(
            summary=RecoveryCodeSetSummary(
                recovery_code_set_id=set_id,
                status="active",
                revision=1,
                remaining_count=10,
                generated_at=now,
            ),
            codes=codes,
        )
    except ApiError:
        _rollback(connection)
        raise
    except (DbError, sqlite3.Error, ValueError, TypeError):
        _rollback(connection)
        raise ApiError("identity.mfa_unavailable") from None
    finally:
        connection.close()


def begin_account_recovery(
    config: AppConfig,
    *,
    session_token: str,
    csrf_token: str,
    current_password: str,
    browser_binding: str,
    request_id: str,
    trace_id: str,
) -> PendingAccountRecovery:
    connection = _connect(config)
    notification_code = ""
    target = ""
    pending: PendingAccountRecovery | None = None
    try:
        connection.execute("BEGIN IMMEDIATE")
        identity, now = authenticate_global_session_in_transaction(
            connection,
            config,
            session_token=session_token,
            csrf_token=csrf_token,
        )
        _verify_current_password(connection, identity.account_id, current_password)
        if connection.execute(
            """SELECT 1 FROM account_mfa_factors
                 WHERE account_id=? AND status='active' LIMIT 1""",
            (identity.account_id,),
        ).fetchone() is None:
            raise ApiError("identity.recovery_unavailable")
        if connection.execute(
            """SELECT 1 FROM recovery_codes code
                 JOIN recovery_code_sets code_set
                   ON code_set.recovery_code_set_id=code.recovery_code_set_id
                  AND code_set.account_id=code.account_id
                WHERE code.account_id=? AND code.status='active'
                  AND code_set.status='active' LIMIT 1""",
            (identity.account_id,),
        ).fetchone() is None:
            raise ApiError("identity.recovery_unavailable")
        notification_code = f"{secrets.randbelow(1_000_000):06d}"
        ready_at = now + timedelta(hours=24)
        expires_at = now + timedelta(hours=48)
        connection.execute(
            """UPDATE mfa_challenges SET status='expired',updated_at=?
                 WHERE account_id=? AND purpose='recovery' AND status='pending'""",
            (_db_time(now), identity.account_id),
        )
        challenge_id = _insert_challenge(
            connection,
            config,
            account_id=identity.account_id,
            global_session_id=identity.session_id,
            purpose="recovery",
            method="recovery_code",
            challenge=notification_code.encode("ascii"),
            browser_binding=browser_binding,
            rp_id=None,
            origin=None,
            now=now,
            ready_at=ready_at,
            expires_at=expires_at,
        )
        _write_security_event(
            connection,
            account_id=identity.account_id,
            actor_account_id=identity.account_id,
            event_type="identity.mfa_changed",
            subject_type="factor",
            subject_id=challenge_id,
            reason_code="recovery_started",
            request_id=request_id,
            trace_id=trace_id,
            payload={
                "schema_version": 1,
                "status": "pending",
                "ready_at": _db_time(ready_at),
                "expires_at": _db_time(expires_at),
            },
            now=now,
        )
        target = identity.account.email
        pending = PendingAccountRecovery(challenge_id, ready_at, expires_at)
        connection.commit()
    except ApiError:
        _rollback(connection)
        raise
    except (DbError, sqlite3.Error, ValueError, TypeError):
        _rollback(connection)
        raise ApiError("identity.mfa_unavailable") from None
    finally:
        connection.close()
    assert pending is not None
    try:
        identity_challenge_delivery(config).deliver(
            target=target,
            code=notification_code,
            purpose="mfa_recovery",
            intent_id=pending.challenge_id,
            challenge_id=pending.challenge_id,
            generation=1,
            expires_at=pending.expires_at,
        )
    except Exception:
        _expire_recovery_challenge(config, pending.challenge_id)
        raise ApiError("identity.delivery_unavailable") from None
    finally:
        notification_code = ""
        target = ""
    try:
        _extend_recovery_session(config, pending.challenge_id)
    except ApiError:
        _expire_recovery_challenge(config, pending.challenge_id)
        raise
    return pending


def complete_account_recovery(
    config: AppConfig,
    *,
    session_token: str,
    csrf_token: str,
    challenge_id: str,
    notification_code: str,
    recovery_code: str,
    browser_binding: str,
    request_id: str,
    trace_id: str,
    user_agent: str | None,
    client_host: str | None,
) -> CompletedAccountRecovery:
    _require_id(challenge_id, "mfc")
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        identity, now = authenticate_global_session_in_transaction(
            connection,
            config,
            session_token=session_token,
            csrf_token=csrf_token,
        )
        challenge = connection.execute(
            """SELECT * FROM mfa_challenges
                 WHERE mfa_challenge_id=? AND account_id=? AND global_session_id=?
                   AND purpose='recovery' AND method='recovery_code'""",
            (challenge_id, identity.account_id, identity.session_id),
        ).fetchone()
        if (
            challenge is None
            or challenge["status"] != "pending"
            or _parse_time(challenge["expires_at"]) <= now
            or not hmac.compare_digest(
                str(challenge["browser_binding_hmac"]),
                _secret_hmac(
                    config, "browser-binding", browser_binding.encode("utf-8")
                ),
            )
        ):
            raise ApiError("identity.recovery_verification_failed")
        if now < _parse_time(challenge["ready_at"]):
            raise ApiError("identity.recovery_not_ready")
        if re.fullmatch(r"[0-9]{6}", notification_code) is None:
            _record_recovery_failure(connection, challenge_id, now)
            connection.commit()
            raise ApiError("identity.recovery_verification_failed")
        expected_notification = _secret_hmac(
            config, "mfa-challenge", notification_code.encode("ascii")
        )
        if not hmac.compare_digest(
            str(challenge["challenge_hmac"]), expected_notification
        ):
            _record_recovery_failure(connection, challenge_id, now)
            connection.commit()
            raise ApiError("identity.recovery_verification_failed")
        try:
            _consume_recovery_code(
                connection, config, identity.account_id, recovery_code, now
            )
        except ApiError:
            _record_recovery_failure(connection, challenge_id, now)
            connection.commit()
            raise ApiError("identity.recovery_verification_failed") from None

        stamp = _db_time(now)
        _consume_challenge(connection, challenge_id, now)
        connection.execute(
            """UPDATE mfa_challenges SET status='expired',updated_at=?
                 WHERE account_id=? AND status='pending'""",
            (stamp, identity.account_id),
        )
        connection.execute(
            """UPDATE webauthn_credentials SET status='revoked',revision=revision+1,
                 revoked_at=?,updated_at=?
               WHERE account_id=? AND status='active'""",
            (stamp, stamp, identity.account_id),
        )
        connection.execute(
            """UPDATE account_mfa_factors SET status='disabled',revision=revision+1,
                 disabled_at=?,updated_at=?
               WHERE account_id=? AND status IN('pending','active')""",
            (stamp, stamp, identity.account_id),
        )
        connection.execute(
            """UPDATE recovery_codes SET status='revoked',updated_at=?
                 WHERE account_id=? AND status='active'""",
            (stamp, identity.account_id),
        )
        connection.execute(
            """UPDATE recovery_code_sets SET status='revoked',revision=revision+1,
                 invalidated_at=COALESCE(invalidated_at,?),updated_at=?
               WHERE account_id=? AND status IN('active','exhausted')""",
            (stamp, stamp, identity.account_id),
        )
        connection.execute(
            """UPDATE trusted_devices SET status='revoked',revision=revision+1,
                 revoked_at=?,updated_at=?
               WHERE account_id=? AND status='active'""",
            (stamp, stamp, identity.account_id),
        )
        connection.execute(
            """UPDATE workspace_sessions SET status='revoked',revoke_generation=revoke_generation+1,
                 revoked_at=?,revoked_reason='identity_recovery_completed',updated_at=?
               WHERE account_id=? AND status='active'""",
            (stamp, stamp, identity.account_id),
        )
        connection.execute(
            """UPDATE step_up_grants SET status='revoked',revision=revision+1,updated_at=?
                 WHERE account_id=? AND status='active'""",
            (stamp, identity.account_id),
        )
        connection.execute(
            """UPDATE global_sessions SET status='revoked',revoke_generation=revoke_generation+1,
                 revoked_at=?,revoked_reason='revision_changed',updated_at=?
               WHERE account_id=? AND status='active'""",
            (stamp, stamp, identity.account_id),
        )
        issued = issue_global_session_in_transaction(
            connection,
            config,
            account_id=identity.account_id,
            now=now,
            user_agent=user_agent,
            client_host=client_host,
        )
        connection.execute(
            """UPDATE global_sessions SET assurance_level='recovery',
                 auth_method='recovery_code',updated_at=?
               WHERE session_id=? AND account_id=? AND status='active'""",
            (stamp, issued.current_session.session_id, identity.account_id),
        )
        _write_security_event(
            connection,
            account_id=identity.account_id,
            actor_account_id=identity.account_id,
            event_type="identity.mfa_changed",
            subject_type="session",
            subject_id=issued.current_session.session_id,
            reason_code="recovery_completed",
            request_id=request_id,
            trace_id=trace_id,
            payload={
                "schema_version": 1,
                "assurance_level": "recovery",
                "old_session_id": identity.session_id,
            },
            now=now,
        )
        connection.commit()
        return CompletedAccountRecovery(
            session_token=issued.session_token,
            csrf_token=issued.csrf_token,
            completed_at=now,
        )
    except ApiError:
        _rollback(connection)
        raise
    except (DbError, sqlite3.Error, ValueError, TypeError, KeyError):
        _rollback(connection)
        raise ApiError("identity.mfa_unavailable") from None
    finally:
        connection.close()


def begin_webauthn_step_up(
    config: AppConfig,
    *,
    session_token: str,
    csrf_token: str,
    origin: str,
    browser_binding: str,
) -> PendingWebAuthn:
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        identity, now = authenticate_global_session_in_transaction(
            connection,
            config,
            session_token=session_token,
            csrf_token=csrf_token,
        )
        rp_id = _webauthn_rp_id(config, origin)
        if connection.execute(
            """SELECT 1 FROM webauthn_credentials
                 WHERE account_id=? AND status='active' LIMIT 1""",
            (identity.account_id,),
        ).fetchone() is None:
            raise ApiError("identity.mfa_required")
        challenge = secrets.token_bytes(32)
        expires_at = now + timedelta(minutes=5)
        challenge_id = _insert_challenge(
            connection,
            config,
            account_id=identity.account_id,
            global_session_id=identity.session_id,
            purpose="step_up",
            challenge=challenge,
            browser_binding=browser_binding,
            rp_id=rp_id,
            origin=origin,
            now=now,
            expires_at=expires_at,
        )
        options = generate_authentication_options(
            rp_id=rp_id,
            challenge=challenge,
            timeout=300_000,
            user_verification=UserVerificationRequirement.REQUIRED,
        )
        connection.commit()
        return PendingWebAuthn(
            factor_id="",
            challenge_id=challenge_id,
            public_key_options=json.loads(options_to_json(options)),
            expires_at=expires_at,
        )
    except ApiError:
        _rollback(connection)
        raise
    except (DbError, sqlite3.Error, WebAuthnException, ValueError, TypeError):
        _rollback(connection)
        raise ApiError("identity.mfa_unavailable") from None
    finally:
        connection.close()


def issue_step_up_grant(
    config: AppConfig,
    *,
    session_token: str,
    csrf_token: str,
    workspace_session_token: str | None,
    workspace_id: str | None,
    action_code: str,
    resource_type: str,
    resource_id: str,
    method: str,
    proof: str | dict[str, object],
    challenge_id: str | None,
    browser_binding: str,
    request_id: str,
    trace_id: str,
) -> IssuedStepUpGrant:
    _validate_grant_scope(action_code, resource_type, resource_id)
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        identity, now = authenticate_global_session_in_transaction(
            connection,
            config,
            session_token=session_token,
            csrf_token=csrf_token,
        )
        workspace = _workspace_context(
            connection,
            config,
            token=workspace_session_token,
            workspace_id=workspace_id,
            global_session_id=identity.session_id,
        )
        if workspace is not None and action_code not in workspace.permission_codes:
            raise ApiError("conditional_access.forbidden")
        if method == "totp" and type(proof) is str:
            consume_active_totp_in_transaction(
                connection,
                config,
                account_id=identity.account_id,
                code=proof,
                device_fingerprint=browser_binding,
                now=now,
            )
            assurance = "mfa"
        elif method == "recovery_code" and type(proof) is str:
            _consume_recovery_code(connection, config, identity.account_id, proof, now)
            assurance = "recovery"
        elif method == "webauthn" and type(proof) is dict and challenge_id is not None:
            _consume_webauthn_assertion(
                connection,
                config,
                identity_account_id=identity.account_id,
                global_session_id=identity.session_id,
                challenge_id=challenge_id,
                credential=proof,
                browser_binding=browser_binding,
                now=now,
            )
            assurance = "mfa"
        else:
            raise ApiError("identity.mfa_verification_failed")
        fingerprint = _permission_fingerprint(identity.account_id, identity.session_id, workspace)
        grant = secrets.token_urlsafe(32)
        grant_id = _id("sug")
        expires_at = now + timedelta(minutes=5)
        stamp = _db_time(now)
        connection.execute(
            """INSERT INTO step_up_grants(
                 step_up_grant_id,account_id,global_session_id,workspace_session_id,
                 workspace_id,tenant_id,grant_hmac,action_code,resource_type,resource_id,
                 permission_fingerprint,auth_method,assurance_level,status,revision,
                 expires_at,consumed_at,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'active',1,?,NULL,?,?)""",
            (
                grant_id,
                identity.account_id,
                identity.session_id,
                workspace.workspace_session_id if workspace else None,
                workspace.workspace_id if workspace else None,
                workspace.tenant_id if workspace else None,
                _secret_hmac(config, "step-up-grant", grant.encode("ascii")),
                action_code,
                resource_type,
                resource_id,
                fingerprint,
                method,
                assurance,
                _db_time(expires_at),
                stamp,
                stamp,
            ),
        )
        _raise_session_assurance(
            connection,
            identity.session_id,
            identity.account_id,
            method=method,
            assurance=assurance,
            now=now,
        )
        _write_security_event(
            connection,
            account_id=identity.account_id,
            workspace_id=workspace.workspace_id if workspace else None,
            tenant_id=workspace.tenant_id if workspace else None,
            actor_account_id=identity.account_id,
            event_type="step_up.completed",
            subject_type="grant",
            subject_id=grant_id,
            reason_code="step_up_verified",
            request_id=request_id,
            trace_id=trace_id,
            payload={
                "schema_version": 1,
                "action_code": action_code,
                "resource_type": resource_type,
                "auth_method": method,
            },
            now=now,
        )
        connection.commit()
        return IssuedStepUpGrant(
            grant=grant,
            grant_id=grant_id,
            action_code=action_code,
            resource_type=resource_type,
            resource_id=resource_id,
            auth_method=method,
            assurance_level=assurance,
            expires_at=expires_at,
        )
    except ApiError:
        _rollback(connection)
        raise
    except (DbError, sqlite3.Error, WebAuthnException, ValueError, TypeError, KeyError):
        _rollback(connection)
        raise ApiError("step_up.unavailable") from None
    finally:
        connection.close()


def consume_step_up_grant_in_transaction(
    connection: sqlite3.Connection,
    config: AppConfig,
    *,
    context: WorkspaceSessionContext,
    grant: str,
    action_code: str,
    resource_type: str,
    resource_id: str,
    now: datetime,
) -> str:
    if not connection.in_transaction or len(grant) < 43:
        raise ApiError("step_up.invalid")
    row = connection.execute(
        "SELECT * FROM step_up_grants WHERE grant_hmac=?",
        (_secret_hmac(config, "step-up-grant", grant.encode("ascii")),),
    ).fetchone()
    fingerprint = _permission_fingerprint(context.account_id, context.global_session_id, context)
    if (
        row is None
        or row["status"] != "active"
        or _parse_time(row["expires_at"]) <= now
        or row["account_id"] != context.account_id
        or row["global_session_id"] != context.global_session_id
        or row["workspace_session_id"] != context.workspace_session_id
        or row["workspace_id"] != context.workspace_id
        or row["tenant_id"] != context.tenant_id
        or row["action_code"] != action_code
        or row["resource_type"] != resource_type
        or row["resource_id"] != resource_id
        or not hmac.compare_digest(str(row["permission_fingerprint"]), fingerprint)
    ):
        raise ApiError("step_up.invalid")
    updated = connection.execute(
        """UPDATE step_up_grants
              SET status='consumed',revision=revision+1,consumed_at=?,updated_at=?
            WHERE step_up_grant_id=? AND status='active' AND revision=?""",
        (_db_time(now), _db_time(now), row["step_up_grant_id"], row["revision"]),
    ).rowcount
    if updated != 1:
        raise ApiError("step_up.invalid")
    return str(row["auth_method"])


def consume_global_step_up_grant_in_transaction(
    connection: sqlite3.Connection,
    config: AppConfig,
    *,
    account_id: str,
    global_session_id: str,
    grant: str,
    action_code: str,
    resource_type: str,
    resource_id: str,
    now: datetime,
) -> str:
    """Consume a global step-up grant for an authenticated platform operation."""

    if not connection.in_transaction or len(grant) < 43:
        raise ApiError("step_up.invalid")
    row = connection.execute(
        "SELECT * FROM step_up_grants WHERE grant_hmac=?",
        (_secret_hmac(config, "step-up-grant", grant.encode("ascii")),),
    ).fetchone()
    fingerprint = _permission_fingerprint(account_id, global_session_id, None)
    if (
        row is None
        or row["status"] != "active"
        or _parse_time(row["expires_at"]) <= now
        or row["account_id"] != account_id
        or row["global_session_id"] != global_session_id
        or row["workspace_session_id"] is not None
        or row["workspace_id"] is not None
        or row["tenant_id"] is not None
        or row["action_code"] != action_code
        or row["resource_type"] != resource_type
        or row["resource_id"] != resource_id
        or not hmac.compare_digest(str(row["permission_fingerprint"]), fingerprint)
    ):
        raise ApiError("step_up.invalid")
    updated = connection.execute(
        """UPDATE step_up_grants
              SET status='consumed',revision=revision+1,consumed_at=?,updated_at=?
            WHERE step_up_grant_id=? AND status='active' AND revision=?""",
        (_db_time(now), _db_time(now), row["step_up_grant_id"], row["revision"]),
    ).rowcount
    if updated != 1:
        raise ApiError("step_up.invalid")
    return str(row["auth_method"])


def get_identity_security_overview(
    config: AppConfig, *, session_token: str
) -> IdentitySecurityOverview:
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        identity, now = authenticate_global_session_in_transaction(
            connection, config, session_token=session_token, touch=True
        )
        stamp = _db_time(now)
        connection.execute(
            """UPDATE trusted_devices
                  SET status='expired',revision=revision+1,revoked_at=?,updated_at=?
                WHERE account_id=? AND status='active' AND expires_at<=?""",
            (stamp, stamp, identity.account_id, stamp),
        )
        factors = tuple(
            MfaFactorSummary(
                factor_id=row["factor_id"],
                factor_type=row["factor_type"],
                status=row["status"],
                revision=row["revision"],
                verified_at=_optional_time(row["verified_at"]),
                created_at=_parse_time(row["created_at"]),
            )
            for row in connection.execute(
                """SELECT factor_id,factor_type,status,revision,verified_at,created_at
                     FROM account_mfa_factors WHERE account_id=?
                     ORDER BY status='active' DESC,created_at,factor_id""",
                (identity.account_id,),
            )
        )
        set_row = connection.execute(
            """SELECT * FROM recovery_code_sets WHERE account_id=?
                 ORDER BY generated_at DESC,recovery_code_set_id DESC LIMIT 1""",
            (identity.account_id,),
        ).fetchone()
        recovery = _recovery_summary(set_row) if set_row is not None else None
        connection.execute(
            """UPDATE mfa_challenges SET status='expired',updated_at=?
                 WHERE account_id=? AND purpose='recovery' AND status='pending'
                   AND expires_at<=?""",
            (stamp, identity.account_id, stamp),
        )
        challenge_row = connection.execute(
            """SELECT * FROM mfa_challenges
                 WHERE account_id=? AND global_session_id=? AND purpose='recovery'
                   AND status='pending'
                 ORDER BY created_at DESC,mfa_challenge_id DESC LIMIT 1""",
            (identity.account_id, identity.session_id),
        ).fetchone()
        recovery_challenge = (
            _recovery_challenge_summary(challenge_row)
            if challenge_row is not None
            else None
        )
        devices = tuple(
            _trusted_device_summary(row)
            for row in connection.execute(
                """SELECT * FROM trusted_devices WHERE account_id=?
                     ORDER BY status='active' DESC,created_at DESC,trusted_device_id""",
                (identity.account_id,),
            )
        )
        sessions = tuple(
            _global_session_summary(row, identity.session_id)
            for row in connection.execute(
                """SELECT * FROM global_sessions WHERE account_id=?
                       AND status='active' AND expires_at>? AND idle_expires_at>?
                     ORDER BY CASE WHEN session_id=? THEN 0 ELSE 1 END,created_at DESC""",
                (identity.account_id, stamp, stamp, identity.session_id),
            )
        )
        connection.commit()
        return IdentitySecurityOverview(
            factors, recovery, recovery_challenge, devices, sessions
        )
    except ApiError:
        _rollback(connection)
        raise
    except (DbError, sqlite3.Error, ValueError, TypeError, KeyError):
        _rollback(connection)
        raise ApiError("identity.mfa_unavailable") from None
    finally:
        connection.close()


def trust_current_device(
    config: AppConfig,
    *,
    session_token: str,
    csrf_token: str,
    code: str,
    browser_binding: str,
    label: str,
    device_family: str,
    request_id: str,
    trace_id: str,
) -> TrustedDeviceSummary:
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        identity, now = authenticate_global_session_in_transaction(
            connection,
            config,
            session_token=session_token,
            csrf_token=csrf_token,
        )
        consume_active_totp_in_transaction(
            connection,
            config,
            account_id=identity.account_id,
            code=code,
            device_fingerprint=browser_binding,
            now=now,
        )
        digest = _secret_hmac(config, "trusted-device", browser_binding.encode("utf-8"))
        expires_at = now + timedelta(days=30)
        existing = connection.execute(
            "SELECT * FROM trusted_devices WHERE account_id=? AND device_hmac=?",
            (identity.account_id, digest),
        ).fetchone()
        if existing is None:
            device_id = _id("trd")
            connection.execute(
                """INSERT INTO trusted_devices(
                     trusted_device_id,account_id,device_hmac,label,device_family,trust_level,
                     status,revision,expires_at,last_used_at,revoked_at,created_at,updated_at)
                   VALUES(?,?,?,?,?,'mfa_verified','active',1,?,?,NULL,?,?)""",
                (
                    device_id,
                    identity.account_id,
                    digest,
                    label,
                    device_family,
                    _db_time(expires_at),
                    _db_time(now),
                    _db_time(now),
                    _db_time(now),
                ),
            )
        else:
            device_id = str(existing["trusted_device_id"])
            connection.execute(
                """UPDATE trusted_devices SET label=?,device_family=?,status='active',
                     revision=revision+1,expires_at=?,last_used_at=?,revoked_at=NULL,updated_at=?
                   WHERE trusted_device_id=? AND account_id=?""",
                (
                    label,
                    device_family,
                    _db_time(expires_at),
                    _db_time(now),
                    _db_time(now),
                    device_id,
                    identity.account_id,
                ),
            )
        connection.execute(
            """UPDATE global_sessions SET trusted_device_id=?,assurance_level='mfa',
                 auth_method='totp',updated_at=? WHERE session_id=? AND account_id=?""",
            (device_id, _db_time(now), identity.session_id, identity.account_id),
        )
        row = connection.execute(
            "SELECT * FROM trusted_devices WHERE trusted_device_id=?", (device_id,)
        ).fetchone()
        _write_security_event(
            connection,
            account_id=identity.account_id,
            actor_account_id=identity.account_id,
            event_type="identity.mfa_changed",
            subject_type="device",
            subject_id=device_id,
            reason_code="device_trusted",
            request_id=request_id,
            trace_id=trace_id,
            payload={"schema_version": 1, "device_family": device_family},
            now=now,
        )
        connection.commit()
        return _trusted_device_summary(row)
    except ApiError:
        _rollback(connection)
        raise
    except (DbError, sqlite3.Error, ValueError, TypeError, KeyError):
        _rollback(connection)
        raise ApiError("identity.mfa_unavailable") from None
    finally:
        connection.close()


def revoke_trusted_device(
    config: AppConfig,
    *,
    session_token: str,
    csrf_token: str,
    trusted_device_id: str,
    request_id: str,
    trace_id: str,
) -> None:
    _require_id(trusted_device_id, "trd")
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        identity, now = authenticate_global_session_in_transaction(
            connection,
            config,
            session_token=session_token,
            csrf_token=csrf_token,
        )
        updated = connection.execute(
            """UPDATE trusted_devices SET status='revoked',revision=revision+1,
                 revoked_at=?,updated_at=?
               WHERE trusted_device_id=? AND account_id=? AND status='active'""",
            (
                _db_time(now),
                _db_time(now),
                trusted_device_id,
                identity.account_id,
            ),
        ).rowcount
        if updated != 1:
            raise ApiError("identity.device_not_found")
        connection.execute(
            """UPDATE global_sessions SET trusted_device_id=NULL,assurance_level='password',
                 auth_method='password',revoke_generation=revoke_generation+1,updated_at=?
               WHERE account_id=? AND trusted_device_id=? AND status='active'""",
            (_db_time(now), identity.account_id, trusted_device_id),
        )
        _write_security_event(
            connection,
            account_id=identity.account_id,
            actor_account_id=identity.account_id,
            event_type="identity.mfa_changed",
            subject_type="device",
            subject_id=trusted_device_id,
            reason_code="device_revoked",
            request_id=request_id,
            trace_id=trace_id,
            payload={"schema_version": 1},
            now=now,
        )
        connection.commit()
    except ApiError:
        _rollback(connection)
        raise
    except (DbError, sqlite3.Error, ValueError, TypeError):
        _rollback(connection)
        raise ApiError("identity.mfa_unavailable") from None
    finally:
        connection.close()


def disable_mfa_factor(
    config: AppConfig,
    *,
    session_token: str,
    csrf_token: str,
    factor_id: str,
    current_password: str,
    request_id: str,
    trace_id: str,
) -> None:
    _require_id(factor_id, "mfa")
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        identity, now = authenticate_global_session_in_transaction(
            connection,
            config,
            session_token=session_token,
            csrf_token=csrf_token,
        )
        _verify_current_password(connection, identity.account_id, current_password)
        if identity.current_session.assurance_level != "mfa":
            raise ApiError("identity.mfa_required")
        factor = connection.execute(
            """SELECT * FROM account_mfa_factors
                 WHERE factor_id=? AND account_id=? AND status='active'""",
            (factor_id, identity.account_id),
        ).fetchone()
        if factor is None:
            raise ApiError("identity.mfa_verification_failed")
        if connection.execute(
            """SELECT COUNT(*) FROM account_mfa_factors
                 WHERE account_id=? AND status='active' AND factor_id!=?""",
            (identity.account_id, factor_id),
        ).fetchone()[0] < 1:
            raise ApiError("identity.mfa_required")
        stamp = _db_time(now)
        if factor["factor_type"] == "webauthn":
            connection.execute(
                """UPDATE webauthn_credentials SET status='revoked',revision=revision+1,
                     revoked_at=?,updated_at=?
                   WHERE factor_id=? AND account_id=? AND status='active'""",
                (stamp, stamp, factor_id, identity.account_id),
            )
        connection.execute(
            """UPDATE account_mfa_factors SET status='disabled',revision=revision+1,
                 disabled_at=?,updated_at=?
               WHERE factor_id=? AND account_id=? AND status='active'""",
            (stamp, stamp, factor_id, identity.account_id),
        )
        connection.execute(
            """UPDATE global_sessions SET assurance_level='password',auth_method='password',
                 trusted_device_id=NULL,revoke_generation=revoke_generation+1,updated_at=?
               WHERE account_id=? AND status='active'""",
            (stamp, identity.account_id),
        )
        connection.execute(
            """UPDATE step_up_grants SET status='revoked',revision=revision+1,updated_at=?
                 WHERE account_id=? AND status='active'""",
            (stamp, identity.account_id),
        )
        _write_security_event(
            connection,
            account_id=identity.account_id,
            actor_account_id=identity.account_id,
            event_type="identity.mfa_changed",
            subject_type="factor",
            subject_id=factor_id,
            reason_code="factor_disabled",
            request_id=request_id,
            trace_id=trace_id,
            payload={"schema_version": 1, "factor_type": factor["factor_type"]},
            now=now,
        )
        connection.commit()
    except ApiError:
        _rollback(connection)
        raise
    except (DbError, sqlite3.Error, ValueError, TypeError, KeyError):
        _rollback(connection)
        raise ApiError("identity.mfa_unavailable") from None
    finally:
        connection.close()


def _consume_webauthn_assertion(
    connection: sqlite3.Connection,
    config: AppConfig,
    *,
    identity_account_id: str,
    global_session_id: str,
    challenge_id: str,
    credential: dict[str, object],
    browser_binding: str,
    now: datetime,
) -> None:
    _require_id(challenge_id, "mfc")
    challenge = _pending_challenge(
        connection,
        config,
        challenge_id=challenge_id,
        account_id=identity_account_id,
        global_session_id=global_session_id,
        purpose="step_up",
        browser_binding=browser_binding,
        credential=credential,
        now=now,
    )
    _validate_extensions(credential, allowed=set())
    raw_challenge = _credential_challenge(credential)
    credential_id = _credential_id(credential)
    row = connection.execute(
        """SELECT * FROM webauthn_credentials
             WHERE account_id=? AND credential_id_hmac=? AND status='active'""",
        (
            identity_account_id,
            _secret_hmac(config, "webauthn-credential", credential_id),
        ),
    ).fetchone()
    if row is None:
        raise ApiError("identity.mfa_verification_failed")
    verified = verify_authentication_response(
        credential=credential,
        expected_challenge=raw_challenge,
        expected_rp_id=str(challenge["rp_id"]),
        expected_origin=str(challenge["origin"]),
        credential_public_key=_b64url_decode(str(row["public_key_cose_b64"])),
        credential_current_sign_count=int(row["sign_count"]),
        require_user_verification=True,
    )
    if int(row["sign_count"]) > 0 and verified.new_sign_count <= int(row["sign_count"]):
        raise ApiError("identity.mfa_verification_failed")
    updated = connection.execute(
        """UPDATE webauthn_credentials SET sign_count=?,revision=revision+1,
             last_used_at=?,updated_at=?
           WHERE webauthn_credential_id=? AND revision=? AND status='active'""",
        (
            verified.new_sign_count,
            _db_time(now),
            _db_time(now),
            row["webauthn_credential_id"],
            row["revision"],
        ),
    ).rowcount
    if updated != 1:
        raise ApiError("identity.mfa_verification_failed")
    _consume_challenge(connection, challenge_id, now)


def _consume_recovery_code(
    connection: sqlite3.Connection,
    config: AppConfig,
    account_id: str,
    code: str,
    now: datetime,
) -> None:
    normalized = code.strip().upper()
    if re.fullmatch(r"MHRC-[2-9A-HJ-NP-Z]{5}-[2-9A-HJ-NP-Z]{5}", normalized) is None:
        raise ApiError("identity.mfa_verification_failed")
    prefix = _secret_hmac(config, "recovery-code", normalized.encode("ascii"))
    row = connection.execute(
        """SELECT code.*,code_set.revision set_revision,code_set.generated_count,
                  code_set.consumed_count
             FROM recovery_codes code JOIN recovery_code_sets code_set
               ON code_set.recovery_code_set_id=code.recovery_code_set_id
              AND code_set.account_id=code.account_id
            WHERE code.account_id=? AND code.status='active' AND code_set.status='active'
              AND code.code_hash LIKE ?""",
        (account_id, prefix + "$%"),
    ).fetchone()
    if row is None or not verify_password(normalized, str(row["code_hash"]).split("$", 1)[1]):
        raise ApiError("identity.mfa_verification_failed")
    stamp = _db_time(now)
    if connection.execute(
        """UPDATE recovery_codes SET status='consumed',consumed_at=?,updated_at=?
             WHERE recovery_code_id=? AND status='active'""",
        (stamp, stamp, row["recovery_code_id"]),
    ).rowcount != 1:
        raise ApiError("identity.mfa_verification_failed")
    consumed = int(row["consumed_count"]) + 1
    exhausted = consumed >= int(row["generated_count"])
    connection.execute(
        """UPDATE recovery_code_sets SET consumed_count=?,status=?,revision=revision+1,
             invalidated_at=?,updated_at=?
           WHERE recovery_code_set_id=? AND revision=? AND status='active'""",
        (
            consumed,
            "exhausted" if exhausted else "active",
            stamp if exhausted else None,
            stamp,
            row["recovery_code_set_id"],
            row["set_revision"],
        ),
    )


def _pending_challenge(
    connection: sqlite3.Connection,
    config: AppConfig,
    *,
    challenge_id: str,
    account_id: str,
    global_session_id: str,
    purpose: str,
    browser_binding: str,
    credential: dict[str, object],
    now: datetime,
) -> sqlite3.Row:
    row = connection.execute(
        """SELECT * FROM mfa_challenges
             WHERE mfa_challenge_id=? AND account_id=? AND global_session_id=?
               AND purpose=?""",
        (challenge_id, account_id, global_session_id, purpose),
    ).fetchone()
    if (
        row is None
        or row["status"] != "pending"
        or _parse_time(row["expires_at"]) <= now
        or not hmac.compare_digest(
            str(row["browser_binding_hmac"]),
            _secret_hmac(config, "browser-binding", browser_binding.encode("utf-8")),
        )
    ):
        raise ApiError("identity.mfa_verification_failed")
    raw_challenge = _credential_challenge(credential)
    if not hmac.compare_digest(
        str(row["challenge_hmac"]),
        _secret_hmac(config, "mfa-challenge", raw_challenge),
    ):
        connection.execute(
            """UPDATE mfa_challenges SET attempt_count=CASE WHEN attempt_count<8 THEN attempt_count+1 ELSE 8 END,
                 status=CASE WHEN attempt_count+1>=8 THEN 'exhausted' ELSE status END,
                 updated_at=? WHERE mfa_challenge_id=?""",
            (_db_time(now), challenge_id),
        )
        raise ApiError("identity.mfa_verification_failed")
    return row


def _insert_challenge(
    connection: sqlite3.Connection,
    config: AppConfig,
    *,
    account_id: str,
    global_session_id: str,
    purpose: str,
    method: str = "webauthn",
    challenge: bytes,
    browser_binding: str,
    rp_id: str | None,
    origin: str | None,
    now: datetime,
    ready_at: datetime | None = None,
    expires_at: datetime,
) -> str:
    connection.execute(
        """UPDATE mfa_challenges SET status='expired',updated_at=?
             WHERE account_id=? AND global_session_id=? AND purpose=? AND status='pending'""",
        (_db_time(now), account_id, global_session_id, purpose),
    )
    challenge_id = _id("mfc")
    connection.execute(
        """INSERT INTO mfa_challenges(
             mfa_challenge_id,account_id,global_session_id,workspace_session_id,purpose,
             method,challenge_hmac,browser_binding_hmac,rp_id,origin,status,attempt_count,
             ready_at,expires_at,consumed_at,created_at,updated_at)
           VALUES(?,?,?,NULL,?,?,?,?,?,?, 'pending',0,?,?,NULL,?,?)""",
        (
            challenge_id,
            account_id,
            global_session_id,
            purpose,
            method,
            _secret_hmac(config, "mfa-challenge", challenge),
            _secret_hmac(config, "browser-binding", browser_binding.encode("utf-8")),
            rp_id,
            origin,
            _db_time(ready_at or now),
            _db_time(expires_at),
            _db_time(now),
            _db_time(now),
        ),
    )
    return challenge_id


def _consume_challenge(connection: sqlite3.Connection, challenge_id: str, now: datetime) -> None:
    if connection.execute(
        """UPDATE mfa_challenges SET status='consumed',consumed_at=?,updated_at=?
             WHERE mfa_challenge_id=? AND status='pending'""",
        (_db_time(now), _db_time(now), challenge_id),
    ).rowcount != 1:
        raise ApiError("identity.mfa_verification_failed")


def _record_recovery_failure(
    connection: sqlite3.Connection, challenge_id: str, now: datetime
) -> None:
    connection.execute(
        """UPDATE mfa_challenges SET attempt_count=CASE WHEN attempt_count<8 THEN attempt_count+1 ELSE 8 END,
             status=CASE WHEN attempt_count+1>=8 THEN 'exhausted' ELSE status END,
             updated_at=? WHERE mfa_challenge_id=? AND status='pending'""",
        (_db_time(now), challenge_id),
    )


def _expire_recovery_challenge(config: AppConfig, challenge_id: str) -> None:
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """UPDATE mfa_challenges SET status='expired',updated_at=?
                 WHERE mfa_challenge_id=? AND purpose='recovery' AND status='pending'""",
            (_db_time(datetime.now(UTC)), challenge_id),
        )
        connection.commit()
    except (DbError, sqlite3.Error, ValueError, TypeError):
        _rollback(connection)
    finally:
        connection.close()


def _extend_recovery_session(config: AppConfig, challenge_id: str) -> None:
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        challenge = connection.execute(
            """SELECT account_id,global_session_id,expires_at FROM mfa_challenges
                 WHERE mfa_challenge_id=? AND purpose='recovery' AND status='pending'""",
            (challenge_id,),
        ).fetchone()
        if challenge is None:
            raise ApiError("identity.mfa_unavailable")
        updated = connection.execute(
            """UPDATE global_sessions SET expires_at=?,idle_expires_at=?,updated_at=?
                 WHERE session_id=? AND account_id=? AND status='active'""",
            (
                challenge["expires_at"],
                challenge["expires_at"],
                _db_time(datetime.now(UTC)),
                challenge["global_session_id"],
                challenge["account_id"],
            ),
        ).rowcount
        if updated != 1:
            raise ApiError("identity.mfa_unavailable")
        connection.commit()
    except ApiError:
        _rollback(connection)
        raise
    except (DbError, sqlite3.Error, ValueError, TypeError):
        _rollback(connection)
        raise ApiError("identity.mfa_unavailable") from None
    finally:
        connection.close()


def _workspace_context(
    connection: sqlite3.Connection,
    config: AppConfig,
    *,
    token: str | None,
    workspace_id: str | None,
    global_session_id: str,
) -> WorkspaceSessionContext | None:
    if workspace_id is None:
        if token is not None:
            raise ApiError("identity.request_invalid")
        return None
    if token is None:
        raise ApiError("workspace_session.required")
    context = authenticate_workspace_session_in_transaction(
        connection, config, session_token=token, touch=False
    )
    if context.workspace_id != workspace_id or context.global_session_id != global_session_id:
        raise ApiError("workspace_session.invalid")
    return context


def _permission_fingerprint(
    account_id: str,
    global_session_id: str,
    workspace: WorkspaceSessionContext | None,
) -> str:
    values = [account_id, global_session_id]
    if workspace is not None:
        values.extend(
            [
                workspace.workspace_session_id,
                workspace.workspace_id,
                workspace.tenant_id,
                str(workspace.permission_revision),
                workspace.authorization_fingerprint,
            ]
        )
    return hashlib.sha256(
        json.dumps(values, ensure_ascii=True, separators=(",", ":")).encode("ascii")
    ).hexdigest()


def _verify_current_password(
    connection: sqlite3.Connection, account_id: str, current_password: str
) -> None:
    row = connection.execute(
        """SELECT secret_hash FROM account_credentials
             WHERE account_id=? AND credential_type='password' AND status='active'""",
        (account_id,),
    ).fetchone()
    if row is None or not verify_password(current_password, row["secret_hash"]):
        raise ApiError("identity.reauthentication_failed")


def _webauthn_rp_id(config: AppConfig, origin: str) -> str:
    if origin not in config.allowed_origins:
        raise ApiError("identity.origin_invalid")
    parsed = urlsplit(origin)
    if parsed.username or parsed.password or parsed.query or parsed.fragment or not parsed.hostname:
        raise ApiError("identity.origin_invalid")
    if parsed.scheme != "https" and not (
        parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    ):
        raise ApiError("identity.origin_invalid")
    return parsed.hostname


def _credential_challenge(credential: dict[str, object]) -> bytes:
    response = credential.get("response")
    if type(response) is not dict or type(response.get("clientDataJSON")) is not str:
        raise ApiError("identity.mfa_verification_failed")
    try:
        client_data = json.loads(_b64url_decode(response["clientDataJSON"]).decode("utf-8"))
        challenge = client_data["challenge"]
        if type(challenge) is not str:
            raise ValueError
        return _b64url_decode(challenge)
    except (UnicodeDecodeError, json.JSONDecodeError, KeyError, ValueError, TypeError):
        raise ApiError("identity.mfa_verification_failed") from None


def _credential_id(credential: dict[str, object]) -> bytes:
    value = credential.get("rawId", credential.get("id"))
    if type(value) is not str or len(value) > 2048:
        raise ApiError("identity.mfa_verification_failed")
    try:
        return _b64url_decode(value)
    except ValueError:
        raise ApiError("identity.mfa_verification_failed") from None


def _credential_transports(credential: dict[str, object]) -> tuple[str, ...]:
    response = credential.get("response")
    values = response.get("transports", []) if type(response) is dict else []
    allowed = {"ble", "hybrid", "internal", "nfc", "smart-card", "usb"}
    if type(values) is not list or any(
        type(item) is not str or item not in allowed for item in values
    ):
        raise ApiError("identity.mfa_verification_failed")
    return tuple(sorted(set(values)))


def _validate_extensions(credential: dict[str, object], *, allowed: set[str]) -> None:
    extensions = credential.get("clientExtensionResults", {})
    if type(extensions) is not dict or not set(extensions) <= allowed:
        raise ApiError("identity.mfa_verification_failed")


def _validate_grant_scope(action: str, resource_type: str, resource_id: str) -> None:
    if (
        _ACTION_RE.fullmatch(action) is None
        or len(action) > 80
        or _RESOURCE_TYPE_RE.fullmatch(resource_type) is None
        or len(resource_type) > 60
        or not 1 <= len(resource_id) <= 120
    ):
        raise ApiError("identity.request_invalid")


def _raise_session_assurance(
    connection: sqlite3.Connection,
    session_id: str,
    account_id: str,
    *,
    method: str,
    assurance: str,
    now: datetime,
) -> None:
    connection.execute(
        """UPDATE global_sessions SET assurance_level=?,auth_method=?,updated_at=?
             WHERE session_id=? AND account_id=? AND status='active'""",
        (assurance, method, _db_time(now), session_id, account_id),
    )


def _write_security_event(
    connection: sqlite3.Connection,
    *,
    account_id: str | None,
    actor_account_id: str,
    event_type: str,
    subject_type: str,
    subject_id: str,
    reason_code: str,
    request_id: str,
    trace_id: str,
    payload: dict[str, object],
    now: datetime,
    workspace_id: str | None = None,
    tenant_id: str | None = None,
) -> None:
    body = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    event_id = _id("ise")
    stamp = _db_time(now)
    connection.execute(
        """INSERT INTO identity_security_events(
             identity_security_event_id,account_id,workspace_id,tenant_id,actor_account_id,
             event_type,subject_type,subject_id,reason_code,request_id,trace_id,payload_json,
             payload_sha256,occurred_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            event_id,
            account_id,
            workspace_id,
            tenant_id,
            actor_account_id,
            event_type,
            subject_type,
            subject_id,
            reason_code,
            request_id,
            trace_id,
            body,
            hashlib.sha256(body.encode("utf-8")).hexdigest(),
            stamp,
        ),
    )
    connection.execute(
        """INSERT INTO identity_security_event_outbox(
             identity_security_event_outbox_id,identity_security_event_id,status,
             attempt_count,next_attempt_at,delivered_at,created_at,updated_at)
           VALUES(?,?,'pending',0,?,NULL,?,?)""",
        (_id("iso"), event_id, stamp, stamp, stamp),
    )


def _global_session_summary(row: sqlite3.Row, current_session_id: str) -> GlobalSessionSummary:
    return GlobalSessionSummary(
        session_id=row["session_id"],
        device_family=row["device_family"],
        created_at=_parse_time(row["created_at"]),
        last_seen_at=_parse_time(row["last_seen_at"] or row["created_at"]),
        expires_at=_parse_time(row["expires_at"]),
        is_current=row["session_id"] == current_session_id,
        assurance_level=row["assurance_level"],
        auth_method=row["auth_method"],
        trusted_device_id=row["trusted_device_id"],
        revoke_generation=row["revoke_generation"],
    )


def _recovery_summary(row: sqlite3.Row) -> RecoveryCodeSetSummary:
    return RecoveryCodeSetSummary(
        recovery_code_set_id=row["recovery_code_set_id"],
        status=row["status"],
        revision=row["revision"],
        remaining_count=int(row["generated_count"]) - int(row["consumed_count"]),
        generated_at=_parse_time(row["generated_at"]),
    )


def _recovery_challenge_summary(row: sqlite3.Row) -> RecoveryChallengeSummary:
    return RecoveryChallengeSummary(
        challenge_id=row["mfa_challenge_id"],
        ready_at=_parse_time(row["ready_at"]),
        expires_at=_parse_time(row["expires_at"]),
    )


def _trusted_device_summary(row: sqlite3.Row) -> TrustedDeviceSummary:
    return TrustedDeviceSummary(
        trusted_device_id=row["trusted_device_id"],
        label=row["label"],
        device_family=row["device_family"],
        status=row["status"],
        revision=row["revision"],
        expires_at=_parse_time(row["expires_at"]),
        last_used_at=_optional_time(row["last_used_at"]),
        created_at=_parse_time(row["created_at"]),
    )


def _recovery_code() -> str:
    raw = "".join(secrets.choice(_RECOVERY_ALPHABET) for _ in range(10))
    return f"MHRC-{raw[:5]}-{raw[5:]}"


def _secret_hmac(config: AppConfig, domain: str, value: bytes) -> str:
    return hmac.new(
        config.identity_session_secret.encode("utf-8"),
        b"metahub:prd116:" + domain.encode("ascii") + b"\x00" + value,
        hashlib.sha256,
    ).hexdigest()


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64url_decode(value: str) -> bytes:
    if type(value) is not str or len(value) > 8192 or not value:
        raise ValueError("base64url")
    return base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def _require_id(value: str, prefix: str) -> None:
    if _ID_RE.fullmatch(value) is None or not value.startswith(prefix + "_"):
        raise ApiError("identity.mfa_verification_failed")


def _connect(config: AppConfig) -> sqlite3.Connection:
    try:
        return connect_database(config.database_url)
    except DbError:
        raise ApiError("identity.mfa_unavailable") from None


def _rollback(connection: sqlite3.Connection) -> None:
    if connection.in_transaction:
        connection.rollback()


def _optional_time(value: str | None) -> datetime | None:
    return _parse_time(value) if value is not None else None


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _db_time(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
