"""Self-service registration, verification, and password recovery for Global Identity."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from metahub_shared.db.connection import connect_database
from metahub_shared.db.errors import DbError
from metahub_shared.db.security import hash_password_argon2id, hash_token, verify_password
from metahub_shared.identity.crypto import (
    IdentityPiiCryptoError,
    IdentityPiiOwner,
    decrypt_identity_pii,
    encrypt_identity_pii,
)
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError

from .delivery import (
    IdentityChallengeDelivery,
    identity_challenge_delivery,
)
from .service import (
    IssuedIdentitySession,
    issue_global_session_in_transaction,
    normalize_global_email,
    replace_global_password_in_transaction,
    validate_global_password,
)

_REGISTRATION_INTENT_RE = re.compile(r"^rgi_[0-9a-f]{32}$")
_RESET_INTENT_RE = re.compile(r"^pri_[0-9a-f]{32}$")
_INVITATION_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")
_CODE_RE = re.compile(r"^[0-9]{6}$")
_INTENT_TTL = timedelta(minutes=15)
_RESEND_COOLDOWN = timedelta(seconds=60)
_RATE_WINDOW = timedelta(minutes=5)
_RATE_RETENTION = timedelta(minutes=10)
_RATE_LIMITS = {
    "registration_email": 5,
    "registration_client": 20,
    "registration_device": 15,
    "registration_global": 500,
    "verification_intent": 10,
    "verification_client": 60,
    "verification_device": 40,
    "verification_global": 1_000,
    "reset_email": 5,
    "reset_client": 20,
    "reset_device": 15,
    "reset_global": 500,
}


@dataclass(frozen=True, slots=True)
class PendingIdentityChallenge:
    intent_id: str
    purpose: str
    status: str
    expires_at: datetime
    resend_available_at: datetime


@dataclass(frozen=True, slots=True)
class _CreatedChallenge:
    pending: PendingIdentityChallenge
    challenge_id: str
    generation: int
    code: str
    target: str


def begin_registration(
    config: AppConfig,
    *,
    email: str,
    display_name: str,
    password: str,
    consent_version: str,
    invitation_token: str | None,
    request_id: str,
    trace_id: str,
    user_agent: str | None,
    client_host: str | None,
    device_token: str,
    delivery: IdentityChallengeDelivery | None = None,
) -> PendingIdentityChallenge:
    normalized_email = normalize_global_email(email)
    if normalized_email is None:
        raise ApiError("identity.registration_invalid")
    normalized_name = _normalize_display_name(display_name)
    normalized_consent = _normalize_consent_version(consent_version)
    validate_global_password(password)
    _consume_rate_limits(
        config,
        action="registration",
        primary_key=normalized_email,
        user_agent=user_agent,
        client_host=client_host,
        device_token=device_token,
    )

    connection = _connect(config)
    try:
        pre_candidates = _account_candidates(connection, normalized_email)
        proof = _registration_proof_candidate(pre_candidates, _utcnow())
        candidate_hash = hash_password_argon2id(password)
        password_matches = (
            verify_password(password, proof["secret_hash"])
            if proof is not None
            else verify_password(password, candidate_hash)
        )

        connection.execute("BEGIN IMMEDIATE")
        now = _utcnow()
        current_candidates = _account_candidates(connection, normalized_email)
        current_proof = _registration_proof_candidate(current_candidates, now)
        if not pre_candidates and not current_candidates:
            intent_kind = "new_account"
            target_account_id = None
            target_credential_revision = None
            stored_candidate_hash = candidate_hash
        elif (
            proof is not None
            and current_proof is not None
            and password_matches
            and current_proof["account_id"] == proof["account_id"]
            and current_proof["credential_revision"] == proof["credential_revision"]
            and hmac.compare_digest(current_proof["secret_hash"], proof["secret_hash"])
        ):
            intent_kind = "existing_account"
            target_account_id = current_proof["account_id"]
            target_credential_revision = int(current_proof["credential_revision"])
            stored_candidate_hash = None
        else:
            intent_kind = "sink"
            target_account_id = None
            target_credential_revision = None
            stored_candidate_hash = candidate_hash

        email_hmac = _secret_hmac(config, "email-identifier", normalized_email)
        _supersede_registration_intents(connection, email_hmac, now)
        invitation = _available_invitation(connection, invitation_token, normalized_email, now)
        created = _insert_registration_challenge(
            connection,
            config,
            email=normalized_email,
            email_hmac=email_hmac,
            display_name=normalized_name,
            candidate_password_hash=stored_candidate_hash,
            intent_kind=intent_kind,
            target_account_id=target_account_id,
            target_credential_revision=target_credential_revision,
            invitation_id=invitation["invitation_id"] if invitation is not None else None,
            consent_version=normalized_consent,
            now=now,
        )
        connection.commit()
    except ApiError:
        if connection.in_transaction:
            connection.rollback()
        raise
    except (sqlite3.Error, DbError, OSError, TypeError, ValueError):
        if connection.in_transaction:
            connection.rollback()
        raise ApiError("identity.unavailable") from None
    finally:
        connection.close()

    _dispatch_challenge(
        config,
        created,
        request_id=request_id,
        trace_id=trace_id,
        delivery=delivery,
        invalidate_intent_on_failure=True,
    )
    return created.pending


def resend_registration_verification(
    config: AppConfig,
    *,
    intent_id: str,
    request_id: str,
    trace_id: str,
    user_agent: str | None,
    client_host: str | None,
    device_token: str,
    delivery: IdentityChallengeDelivery | None = None,
) -> PendingIdentityChallenge:
    _require_intent_id(intent_id, purpose="registration")
    _consume_rate_limits(
        config,
        action="verification",
        primary_key=intent_id,
        user_agent=user_agent,
        client_host=client_host,
        device_token=device_token,
    )
    now = _utcnow()
    synthetic = PendingIdentityChallenge(
        intent_id=intent_id,
        purpose="registration",
        status="pending",
        expires_at=now + _INTENT_TTL,
        resend_available_at=now + _RESEND_COOLDOWN,
    )
    connection = _connect(config)
    created: _CreatedChallenge | None = None
    try:
        connection.execute("BEGIN IMMEDIATE")
        now = _utcnow()
        intent = connection.execute(
            "SELECT * FROM registration_intents WHERE registration_intent_id=?",
            (intent_id,),
        ).fetchone()
        if intent is None or intent["status"] != "pending":
            connection.commit()
            return synthetic
        expires_at = _parse_time(intent["expires_at"])
        if expires_at <= now:
            _expire_registration_intent(connection, intent_id, now)
            connection.commit()
            return synthetic
        latest = connection.execute(
            """
            SELECT * FROM verification_challenges
            WHERE registration_intent_id=? ORDER BY generation DESC LIMIT 1
            """,
            (intent_id,),
        ).fetchone()
        if latest is not None:
            retry_at = _parse_time(latest["sent_at"]) + _RESEND_COOLDOWN
            if latest["status"] == "pending" and retry_at > now:
                connection.commit()
                return PendingIdentityChallenge(
                    intent_id=intent_id,
                    purpose="registration",
                    status="pending",
                    expires_at=expires_at,
                    resend_available_at=retry_at,
                )
            connection.execute(
                """
                UPDATE verification_challenges SET status='superseded',updated_at=?
                WHERE registration_intent_id=? AND status='pending'
                """,
                (_db_time(now), intent_id),
            )
        generation = int(intent["generation"]) + 1
        connection.execute(
            """
            UPDATE registration_intents SET generation=?,updated_at=?
            WHERE registration_intent_id=?
            """,
            (generation, _db_time(now), intent_id),
        )
        registration_email = _registration_email(config, intent)
        created = _insert_challenge(
            connection,
            config,
            purpose="registration",
            intent_id=intent_id,
            generation=generation,
            target=registration_email,
            expires_at=expires_at,
            now=now,
        )
        connection.commit()
    except ApiError:
        if connection.in_transaction:
            connection.rollback()
        raise
    except (sqlite3.Error, DbError, OSError, TypeError, ValueError):
        if connection.in_transaction:
            connection.rollback()
        raise ApiError("identity.unavailable") from None
    finally:
        connection.close()
    if created is None:
        return synthetic
    _dispatch_challenge(
        config,
        created,
        request_id=request_id,
        trace_id=trace_id,
        delivery=delivery,
        invalidate_intent_on_failure=False,
    )
    return created.pending


def verify_registration(
    config: AppConfig,
    *,
    intent_id: str,
    code: str,
    request_id: str,
    trace_id: str,
    user_agent: str | None,
    client_host: str | None,
    device_token: str,
) -> IssuedIdentitySession:
    _require_intent_id(intent_id, purpose="registration")
    _require_code(code)
    _consume_rate_limits(
        config,
        action="verification",
        primary_key=intent_id,
        user_agent=user_agent,
        client_host=client_host,
        device_token=device_token,
    )
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        now = _utcnow()
        row = _registration_challenge(connection, intent_id)
        if row is None or row["intent_status"] != "pending" or row["challenge_status"] != "pending":
            raise ApiError("identity.verification_invalid")
        if (
            _parse_time(row["intent_expires_at"]) <= now
            or _parse_time(row["challenge_expires_at"]) <= now
        ):
            _expire_registration_intent(connection, intent_id, now)
            connection.commit()
            raise ApiError("identity.verification_invalid")
        expected = _code_hmac(config, "registration", intent_id, row["generation"], code)
        if not hmac.compare_digest(expected, row["code_hmac"]):
            _record_failed_registration_code(connection, row, now)
            connection.commit()
            raise ApiError("identity.verification_invalid")
        registration_email = _registration_email(config, row)
        if row["intent_kind"] == "sink":
            _consume_registration_sink(connection, row, now)
            connection.commit()
            raise ApiError("identity.verification_invalid")

        if row["intent_kind"] == "existing_account":
            account = connection.execute(
                """
                SELECT account_id,status,login_enabled,credential_revision,locked_until
                FROM global_accounts WHERE account_id=?
                """,
                (row["target_account_id"],),
            ).fetchone()
            account_state_valid = account is not None and (
                account["status"] == "active"
                or (
                    account["status"] == "locked"
                    and account["locked_until"] is not None
                    and _parse_time(account["locked_until"]) <= now
                )
            )
            if (
                account is None
                or not account_state_valid
                or account["login_enabled"] != 1
                or account["credential_revision"] != row["target_credential_revision"]
            ):
                _consume_registration_sink(connection, row, now)
                connection.commit()
                raise ApiError("identity.verification_invalid")
            account_id = account["account_id"]
            if account["status"] == "locked":
                connection.execute(
                    """
                    UPDATE global_accounts
                    SET status='active',failed_login_count=0,locked_until=NULL,updated_at=?
                    WHERE account_id=?
                    """,
                    (_db_time(now), account_id),
                )
            event_type = "identity.verified"
            reason_code = "existing_account_verified"
        else:
            if _account_candidates(connection, registration_email):
                _consume_registration_sink(connection, row, now)
                connection.commit()
                raise ApiError("identity.verification_invalid")
            account_id = f"gac_{uuid4().hex}"
            credential_id = f"acr_{uuid4().hex}"
            connection.execute(
                """
                INSERT INTO global_accounts(
                  account_id,email_normalized,phone_e164,display_name,status,login_enabled,
                  credential_revision,session_revision,mfa_state,failed_login_count,locked_until,
                  last_login_at,created_at,updated_at)
                VALUES(?,?,NULL,?,'active',1,1,1,'not_enrolled',0,NULL,NULL,?,?)
                """,
                (
                    account_id,
                    registration_email,
                    row["display_name"],
                    _db_time(now),
                    _db_time(now),
                ),
            )
            connection.execute(
                """
                INSERT INTO account_credentials(
                  credential_id,account_id,credential_type,algorithm,secret_hash,status,version,
                  created_at,updated_at,rotated_at)
                VALUES(?,?,'password','argon2id',?,'active',1,?,?,NULL)
                """,
                (
                    credential_id,
                    account_id,
                    row["candidate_password_hash"],
                    _db_time(now),
                    _db_time(now),
                ),
            )
            event_type = "identity.registered"
            reason_code = "self_service_verified"

        _record_account_verification(
            connection,
            config,
            account_id=account_id,
            email=registration_email,
            source_type="registration",
            source_intent_id=intent_id,
            now=now,
        )
        _record_consent(connection, account_id=account_id, row=row, now=now)
        invitation_bound = _record_invitation_binding(
            connection,
            account_id=account_id,
            row=row,
            email=registration_email,
            now=now,
        )
        connection.execute(
            """
            UPDATE verification_challenges
            SET status='consumed',consumed_at=?,updated_at=?
            WHERE verification_challenge_id=? AND status='pending'
            """,
            (_db_time(now), _db_time(now), row["verification_challenge_id"]),
        )
        connection.execute(
            """
            UPDATE registration_intents SET status='verified',verified_at=?,updated_at=?
            WHERE registration_intent_id=? AND status='pending'
            """,
            (_db_time(now), _db_time(now), intent_id),
        )
        _write_lifecycle_event(
            connection,
            account_id=account_id,
            source_intent_id=intent_id,
            event_type=event_type,
            reason_code=reason_code,
            request_id=request_id,
            trace_id=trace_id,
            payload={
                "schema_version": 1,
                "generation": int(row["generation"]),
                "invitation_binding_recorded": invitation_bound,
            },
            now=now,
        )
        issued = issue_global_session_in_transaction(
            connection,
            config,
            account_id=account_id,
            now=now,
            user_agent=user_agent,
            client_host=client_host,
        )
        connection.commit()
        return issued
    except ApiError:
        if connection.in_transaction:
            connection.rollback()
        raise
    except (sqlite3.Error, DbError, OSError, TypeError, ValueError):
        if connection.in_transaction:
            connection.rollback()
        raise ApiError("identity.unavailable") from None
    finally:
        connection.close()


def begin_password_reset(
    config: AppConfig,
    *,
    email: str,
    request_id: str,
    trace_id: str,
    user_agent: str | None,
    client_host: str | None,
    device_token: str,
    delivery: IdentityChallengeDelivery | None = None,
) -> PendingIdentityChallenge:
    normalized_email = normalize_global_email(email)
    if normalized_email is None:
        raise ApiError("identity.reset_invalid")
    _consume_rate_limits(
        config,
        action="reset",
        primary_key=normalized_email,
        user_agent=user_agent,
        client_host=client_host,
        device_token=device_token,
    )
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        now = _utcnow()
        candidates = _account_candidates(connection, normalized_email)
        account = _reset_candidate(candidates)
        email_hmac = _secret_hmac(config, "email-identifier", normalized_email)
        _supersede_reset_intents(connection, email_hmac, now)
        intent_id = f"pri_{uuid4().hex}"
        expires_at = now + _INTENT_TTL
        connection.execute(
            """
            INSERT INTO password_reset_intents(
              password_reset_intent_id,email_hmac,intent_kind,target_account_id,
              target_credential_revision,status,generation,expires_at,completed_at,
              created_at,updated_at)
            VALUES(?,?,?,?,?,'pending',1,?,NULL,?,?)
            """,
            (
                intent_id,
                email_hmac,
                "account" if account is not None else "sink",
                account["account_id"] if account is not None else None,
                account["credential_revision"] if account is not None else None,
                _db_time(expires_at),
                _db_time(now),
                _db_time(now),
            ),
        )
        created = _insert_challenge(
            connection,
            config,
            purpose="password_reset",
            intent_id=intent_id,
            generation=1,
            target=normalized_email,
            expires_at=expires_at,
            now=now,
        )
        connection.commit()
    except ApiError:
        if connection.in_transaction:
            connection.rollback()
        raise
    except (sqlite3.Error, DbError, OSError, TypeError, ValueError):
        if connection.in_transaction:
            connection.rollback()
        raise ApiError("identity.unavailable") from None
    finally:
        connection.close()
    _dispatch_challenge(
        config,
        created,
        request_id=request_id,
        trace_id=trace_id,
        delivery=delivery,
        invalidate_intent_on_failure=True,
    )
    return created.pending


def confirm_password_reset(
    config: AppConfig,
    *,
    intent_id: str,
    code: str,
    new_password: str,
    request_id: str,
    trace_id: str,
    user_agent: str | None,
    client_host: str | None,
    device_token: str,
) -> IssuedIdentitySession:
    _require_intent_id(intent_id, purpose="password_reset")
    _require_code(code)
    validate_global_password(new_password)
    _consume_rate_limits(
        config,
        action="verification",
        primary_key=intent_id,
        user_agent=user_agent,
        client_host=client_host,
        device_token=device_token,
    )
    connection = _connect(config)
    try:
        row = _reset_challenge(connection, intent_id)
        now = _utcnow()
        if row is None or not _pending_reset_row(row, now):
            raise ApiError("identity.verification_invalid")
        expected = _code_hmac(config, "password_reset", intent_id, row["generation"], code)
        if not hmac.compare_digest(expected, row["code_hmac"]):
            connection.execute("BEGIN IMMEDIATE")
            locked = _reset_challenge(connection, intent_id)
            now = _utcnow()
            if locked is not None and _pending_reset_row(locked, now):
                locked_expected = _code_hmac(
                    config,
                    "password_reset",
                    intent_id,
                    locked["generation"],
                    code,
                )
                if not hmac.compare_digest(locked_expected, locked["code_hmac"]):
                    _record_failed_reset_code(connection, locked, now)
            connection.commit()
            raise ApiError("identity.verification_invalid")

        new_hash = hash_password_argon2id(new_password)
        connection.execute("BEGIN IMMEDIATE")
        now = _utcnow()
        row = _reset_challenge(connection, intent_id)
        if row is None or not _pending_reset_row(row, now):
            raise ApiError("identity.verification_invalid")
        expected = _code_hmac(config, "password_reset", intent_id, row["generation"], code)
        if not hmac.compare_digest(expected, row["code_hmac"]):
            raise ApiError("identity.verification_invalid")
        if row["intent_kind"] == "sink":
            _consume_reset_sink(connection, row, now)
            connection.commit()
            raise ApiError("identity.verification_invalid")
        account = connection.execute(
            """
            SELECT account_id,status,login_enabled,credential_revision
            FROM global_accounts WHERE account_id=?
            """,
            (row["target_account_id"],),
        ).fetchone()
        if (
            account is None
            or account["login_enabled"] != 1
            or account["status"] not in {"active", "locked"}
            or account["credential_revision"] != row["target_credential_revision"]
        ):
            _consume_reset_sink(connection, row, now)
            connection.commit()
            raise ApiError("identity.verification_invalid")
        account_id = account["account_id"]
        if account["status"] == "locked":
            connection.execute(
                "UPDATE global_accounts SET status='active',updated_at=? WHERE account_id=?",
                (_db_time(now), account_id),
            )
        replace_global_password_in_transaction(
            connection,
            account_id=account_id,
            new_hash=new_hash,
            now=now,
        )
        _record_account_verification(
            connection,
            config,
            account_id=account_id,
            email=row["email_normalized"],
            source_type="password_reset",
            source_intent_id=intent_id,
            now=now,
        )
        connection.execute(
            """
            UPDATE verification_challenges
            SET status='consumed',consumed_at=?,updated_at=?
            WHERE verification_challenge_id=? AND status='pending'
            """,
            (_db_time(now), _db_time(now), row["verification_challenge_id"]),
        )
        connection.execute(
            """
            UPDATE password_reset_intents SET status='completed',completed_at=?,updated_at=?
            WHERE password_reset_intent_id=? AND status='pending'
            """,
            (_db_time(now), _db_time(now), intent_id),
        )
        _write_lifecycle_event(
            connection,
            account_id=account_id,
            source_intent_id=intent_id,
            event_type="identity.password_reset",
            reason_code="email_challenge_verified",
            request_id=request_id,
            trace_id=trace_id,
            payload={"schema_version": 1, "generation": int(row["generation"])},
            now=now,
        )
        issued = issue_global_session_in_transaction(
            connection,
            config,
            account_id=account_id,
            now=now,
            user_agent=user_agent,
            client_host=client_host,
        )
        connection.commit()
        return issued
    except ApiError:
        if connection.in_transaction:
            connection.rollback()
        raise
    except (sqlite3.Error, DbError, OSError, TypeError, ValueError):
        if connection.in_transaction:
            connection.rollback()
        raise ApiError("identity.unavailable") from None
    finally:
        connection.close()


def _insert_registration_challenge(
    connection: sqlite3.Connection,
    config: AppConfig,
    *,
    email: str,
    email_hmac: str,
    display_name: str,
    candidate_password_hash: str | None,
    intent_kind: str,
    target_account_id: str | None,
    target_credential_revision: int | None,
    invitation_id: str | None,
    consent_version: str,
    now: datetime,
) -> _CreatedChallenge:
    intent_id = f"rgi_{uuid4().hex}"
    expires_at = now + _INTENT_TTL
    email_ciphertext = encrypt_identity_pii(
        email,
        secret=config.identity_pii_encryption_secret,
        owner=_registration_email_owner(intent_id),
    )
    connection.execute(
        """
        INSERT INTO registration_intents(
          registration_intent_id,email_ciphertext,email_hmac,display_name,
          candidate_password_hash,intent_kind,target_account_id,target_credential_revision,
          invitation_id,consent_version,status,generation,expires_at,verified_at,created_at,updated_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,'pending',1,?,NULL,?,?)
        """,
        (
            intent_id,
            email_ciphertext,
            email_hmac,
            display_name,
            candidate_password_hash,
            intent_kind,
            target_account_id,
            target_credential_revision,
            invitation_id,
            consent_version,
            _db_time(expires_at),
            _db_time(now),
            _db_time(now),
        ),
    )
    return _insert_challenge(
        connection,
        config,
        purpose="registration",
        intent_id=intent_id,
        generation=1,
        target=email,
        expires_at=expires_at,
        now=now,
    )


def _insert_challenge(
    connection: sqlite3.Connection,
    config: AppConfig,
    *,
    purpose: str,
    intent_id: str,
    generation: int,
    target: str,
    expires_at: datetime,
    now: datetime,
) -> _CreatedChallenge:
    code = f"{secrets.randbelow(1_000_000):06d}"
    challenge_id = f"vch_{uuid4().hex}"
    code_hmac = _code_hmac(config, purpose, intent_id, generation, code)
    registration_id = intent_id if purpose == "registration" else None
    reset_id = intent_id if purpose == "password_reset" else None
    connection.execute(
        """
        INSERT INTO verification_challenges(
          verification_challenge_id,registration_intent_id,password_reset_intent_id,purpose,
          code_hmac,status,generation,attempt_count,sent_at,expires_at,consumed_at,created_at,updated_at)
        VALUES(?,?,?,? ,?,'pending',?,0,?,?,NULL,?,?)
        """,
        (
            challenge_id,
            registration_id,
            reset_id,
            purpose,
            code_hmac,
            generation,
            _db_time(now),
            _db_time(expires_at),
            _db_time(now),
            _db_time(now),
        ),
    )
    pending = PendingIdentityChallenge(
        intent_id=intent_id,
        purpose=purpose,
        status="pending",
        expires_at=expires_at,
        resend_available_at=now + _RESEND_COOLDOWN,
    )
    return _CreatedChallenge(
        pending=pending,
        challenge_id=challenge_id,
        generation=generation,
        code=code,
        target=target,
    )


def _dispatch_challenge(
    config: AppConfig,
    created: _CreatedChallenge,
    *,
    request_id: str,
    trace_id: str,
    delivery: IdentityChallengeDelivery | None,
    invalidate_intent_on_failure: bool,
) -> None:
    adapter = delivery or identity_challenge_delivery(config)
    try:
        adapter.deliver(
            target=created.target,
            code=created.code,
            purpose=created.pending.purpose,
            intent_id=created.pending.intent_id,
            challenge_id=created.challenge_id,
            generation=created.generation,
            expires_at=created.pending.expires_at,
        )
    except Exception:
        _invalidate_failed_dispatch(
            config,
            created,
            request_id=request_id,
            trace_id=trace_id,
            reason_code="delivery_adapter_failed",
            invalidate_intent=invalidate_intent_on_failure,
        )
        raise ApiError("identity.delivery_unavailable") from None
    try:
        _record_challenge_delivery_event(
            config,
            created,
            request_id=request_id,
            trace_id=trace_id,
            event_type="identity.challenge_dispatched",
            reason_code="delivery_adapter_accepted",
        )
    except ApiError:
        _invalidate_failed_dispatch(
            config,
            created,
            request_id=request_id,
            trace_id=trace_id,
            reason_code="delivery_audit_failed",
            invalidate_intent=invalidate_intent_on_failure,
        )
        raise ApiError("identity.delivery_unavailable") from None


def _invalidate_failed_dispatch(
    config: AppConfig,
    created: _CreatedChallenge,
    *,
    request_id: str,
    trace_id: str,
    reason_code: str,
    invalidate_intent: bool,
) -> None:
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        now = _utcnow()
        connection.execute(
            """
            UPDATE verification_challenges SET status='superseded',updated_at=?
            WHERE verification_challenge_id=? AND status='pending'
            """,
            (_db_time(now), created.challenge_id),
        )
        if invalidate_intent and created.pending.purpose == "registration":
            connection.execute(
                """
                UPDATE registration_intents SET status='superseded',updated_at=?
                WHERE registration_intent_id=? AND status='pending'
                """,
                (_db_time(now), created.pending.intent_id),
            )
        elif invalidate_intent:
            connection.execute(
                """
                UPDATE password_reset_intents SET status='expired',updated_at=?
                WHERE password_reset_intent_id=? AND status='pending'
                """,
                (_db_time(now), created.pending.intent_id),
            )
        _write_challenge_delivery_event(
            connection,
            config,
            created,
            request_id=request_id,
            trace_id=trace_id,
            event_type="identity.challenge_delivery_failed",
            reason_code=reason_code,
            now=now,
        )
        connection.commit()
    except (sqlite3.Error, DbError, OSError, TypeError, ValueError):
        if connection.in_transaction:
            connection.rollback()
    finally:
        connection.close()


def _record_challenge_delivery_event(
    config: AppConfig,
    created: _CreatedChallenge,
    *,
    request_id: str,
    trace_id: str,
    event_type: str,
    reason_code: str,
) -> None:
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        _write_challenge_delivery_event(
            connection,
            config,
            created,
            request_id=request_id,
            trace_id=trace_id,
            event_type=event_type,
            reason_code=reason_code,
            now=_utcnow(),
        )
        connection.commit()
    except (sqlite3.Error, DbError, OSError, TypeError, ValueError):
        if connection.in_transaction:
            connection.rollback()
        raise ApiError("identity.unavailable") from None
    finally:
        connection.close()


def _write_challenge_delivery_event(
    connection: sqlite3.Connection,
    config: AppConfig,
    created: _CreatedChallenge,
    *,
    request_id: str,
    trace_id: str,
    event_type: str,
    reason_code: str,
    now: datetime,
) -> None:
    _write_lifecycle_event(
        connection,
        account_id=None,
        source_intent_id=created.pending.intent_id,
        event_type=event_type,
        reason_code=reason_code,
        request_id=request_id,
        trace_id=trace_id,
        payload={
            "schema_version": 1,
            "purpose": created.pending.purpose,
            "challenge_id": created.challenge_id,
            "generation": created.generation,
            "target_hmac": _secret_hmac(config, "delivery-target", created.target),
        },
        now=now,
    )


def _consume_rate_limits(
    config: AppConfig,
    *,
    action: str,
    primary_key: str,
    user_agent: str | None,
    client_host: str | None,
    device_token: str,
) -> None:
    if action == "registration":
        scopes = (
            ("registration_email", primary_key),
            ("registration_client", _client_key(client_host, user_agent)),
            ("registration_device", device_token or _client_key(client_host, user_agent)),
            ("registration_global", "global"),
        )
    elif action == "reset":
        scopes = (
            ("reset_email", primary_key),
            ("reset_client", _client_key(client_host, user_agent)),
            ("reset_device", device_token or _client_key(client_host, user_agent)),
            ("reset_global", "global"),
        )
    else:
        scopes = (
            ("verification_intent", primary_key),
            ("verification_client", _client_key(client_host, user_agent)),
            ("verification_device", device_token or _client_key(client_host, user_agent)),
            ("verification_global", "global"),
        )
    connection = _connect(config)
    blocked = False
    try:
        connection.execute("BEGIN IMMEDIATE")
        now = _utcnow()
        window_seconds = int(_RATE_WINDOW.total_seconds())
        epoch = int(now.timestamp())
        window_started = datetime.fromtimestamp(epoch - (epoch % window_seconds), tz=UTC)
        window_end = window_started + _RATE_WINDOW
        expires_at = window_end + _RATE_RETENTION
        connection.execute(
            """
            DELETE FROM identity_abuse_buckets WHERE rowid IN(
              SELECT rowid FROM identity_abuse_buckets WHERE expires_at<=? LIMIT 50)
            """,
            (_db_time(now),),
        )
        for scope, raw_key in scopes:
            bucket_hmac = _secret_hmac(config, f"abuse:{scope}", raw_key)
            row = connection.execute(
                """
                SELECT identity_abuse_bucket_id,attempt_count,blocked_until
                FROM identity_abuse_buckets
                WHERE scope=? AND bucket_hmac=? AND window_started_at=?
                """,
                (scope, bucket_hmac, _db_time(window_started)),
            ).fetchone()
            if row is None:
                connection.execute(
                    """
                    INSERT INTO identity_abuse_buckets(
                      identity_abuse_bucket_id,scope,bucket_hmac,window_started_at,
                      attempt_count,blocked_until,expires_at,updated_at)
                    VALUES(?,?,?,?,1,NULL,?,?)
                    """,
                    (
                        f"iab_{uuid4().hex}",
                        scope,
                        bucket_hmac,
                        _db_time(window_started),
                        _db_time(expires_at),
                        _db_time(now),
                    ),
                )
                continue
            attempts = int(row["attempt_count"]) + 1
            already_blocked = row["blocked_until"] is not None and _parse_time(
                row["blocked_until"]
            ) > now
            limit_reached = attempts > _RATE_LIMITS[scope]
            blocked = blocked or already_blocked or limit_reached
            connection.execute(
                """
                UPDATE identity_abuse_buckets
                SET attempt_count=?,blocked_until=?,updated_at=?
                WHERE identity_abuse_bucket_id=?
                """,
                (
                    attempts,
                    _db_time(window_end) if already_blocked or limit_reached else None,
                    _db_time(now),
                    row["identity_abuse_bucket_id"],
                ),
            )
        connection.commit()
    except (sqlite3.Error, DbError, OSError, TypeError, ValueError):
        if connection.in_transaction:
            connection.rollback()
        raise ApiError("identity.unavailable") from None
    finally:
        connection.close()
    if blocked:
        raise ApiError("identity.rate_limited")


def _registration_challenge(
    connection: sqlite3.Connection,
    intent_id: str,
) -> sqlite3.Row | None:
    return connection.execute(
        """
        SELECT i.*,i.status intent_status,i.expires_at intent_expires_at,
          c.verification_challenge_id,c.code_hmac,c.status challenge_status,
          c.generation,c.attempt_count,c.expires_at challenge_expires_at
        FROM registration_intents i
        JOIN verification_challenges c ON c.registration_intent_id=i.registration_intent_id
          AND c.generation=i.generation
        WHERE i.registration_intent_id=?
        ORDER BY c.generation DESC LIMIT 1
        """,
        (intent_id,),
    ).fetchone()


def _reset_challenge(
    connection: sqlite3.Connection,
    intent_id: str,
) -> sqlite3.Row | None:
    return connection.execute(
        """
        SELECT i.*,i.status intent_status,i.expires_at intent_expires_at,
          a.email_normalized,
          c.verification_challenge_id,c.code_hmac,c.status challenge_status,
          c.generation,c.attempt_count,c.expires_at challenge_expires_at
        FROM password_reset_intents i
        LEFT JOIN global_accounts a ON a.account_id=i.target_account_id
        JOIN verification_challenges c ON c.password_reset_intent_id=i.password_reset_intent_id
          AND c.generation=i.generation
        WHERE i.password_reset_intent_id=?
        ORDER BY c.generation DESC LIMIT 1
        """,
        (intent_id,),
    ).fetchone()


def _pending_reset_row(row: sqlite3.Row, now: datetime) -> bool:
    return (
        row["intent_status"] == "pending"
        and row["challenge_status"] == "pending"
        and _parse_time(row["intent_expires_at"]) > now
        and _parse_time(row["challenge_expires_at"]) > now
    )


def _record_failed_registration_code(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    now: datetime,
) -> None:
    attempts = int(row["attempt_count"]) + 1
    status = "exhausted" if attempts >= 5 else "pending"
    connection.execute(
        """
        UPDATE verification_challenges SET attempt_count=?,status=?,updated_at=?
        WHERE verification_challenge_id=? AND status='pending'
        """,
        (attempts, status, _db_time(now), row["verification_challenge_id"]),
    )
    if status == "exhausted":
        connection.execute(
            """
            UPDATE registration_intents SET status='expired',updated_at=?
            WHERE registration_intent_id=? AND status='pending'
            """,
            (_db_time(now), row["registration_intent_id"]),
        )


def _record_failed_reset_code(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    now: datetime,
) -> None:
    attempts = int(row["attempt_count"]) + 1
    status = "exhausted" if attempts >= 5 else "pending"
    connection.execute(
        """
        UPDATE verification_challenges SET attempt_count=?,status=?,updated_at=?
        WHERE verification_challenge_id=? AND status='pending'
        """,
        (attempts, status, _db_time(now), row["verification_challenge_id"]),
    )
    if status == "exhausted":
        connection.execute(
            """
            UPDATE password_reset_intents SET status='exhausted',updated_at=?
            WHERE password_reset_intent_id=? AND status='pending'
            """,
            (_db_time(now), row["password_reset_intent_id"]),
        )


def _consume_registration_sink(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    now: datetime,
) -> None:
    connection.execute(
        """
        UPDATE verification_challenges
        SET status='consumed',consumed_at=?,updated_at=?
        WHERE verification_challenge_id=? AND status='pending'
        """,
        (_db_time(now), _db_time(now), row["verification_challenge_id"]),
    )
    connection.execute(
        """
        UPDATE registration_intents SET status='sink_consumed',verified_at=?,updated_at=?
        WHERE registration_intent_id=? AND status='pending'
        """,
        (_db_time(now), _db_time(now), row["registration_intent_id"]),
    )


def _consume_reset_sink(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    now: datetime,
) -> None:
    connection.execute(
        """
        UPDATE verification_challenges
        SET status='consumed',consumed_at=?,updated_at=?
        WHERE verification_challenge_id=? AND status='pending'
        """,
        (_db_time(now), _db_time(now), row["verification_challenge_id"]),
    )
    connection.execute(
        """
        UPDATE password_reset_intents SET status='sink_consumed',completed_at=?,updated_at=?
        WHERE password_reset_intent_id=? AND status='pending'
        """,
        (_db_time(now), _db_time(now), row["password_reset_intent_id"]),
    )


def _record_account_verification(
    connection: sqlite3.Connection,
    config: AppConfig,
    *,
    account_id: str,
    email: str,
    source_type: str,
    source_intent_id: str,
    now: datetime,
) -> None:
    identifier_hmac = _secret_hmac(config, "email-identifier", email)
    existing = connection.execute(
        """
        SELECT 1 FROM account_verifications
        WHERE account_id=? AND verification_type='email' AND identifier_hmac=?
        """,
        (account_id, identifier_hmac),
    ).fetchone()
    if existing is not None:
        return
    connection.execute(
        """
        INSERT INTO account_verifications(
          account_verification_id,account_id,verification_type,identifier_hmac,
          source_type,source_intent_id,verified_at)
        VALUES(?,?,'email',?,?,?,?)
        """,
        (
            f"avf_{uuid4().hex}",
            account_id,
            identifier_hmac,
            source_type,
            source_intent_id,
            _db_time(now),
        ),
    )


def _record_consent(
    connection: sqlite3.Connection,
    *,
    account_id: str,
    row: sqlite3.Row,
    now: datetime,
) -> None:
    payload = _json_text(
        {
            "consent_type": "privacy_terms",
            "consent_version": row["consent_version"],
            "source": "self_service_registration",
        }
    )
    connection.execute(
        """
        INSERT INTO consent_records(
          consent_record_id,account_id,consent_type,consent_version,
          source_registration_intent_id,payload_sha256,consented_at)
        VALUES(?,?,'privacy_terms',?,?,?,?)
        """,
        (
            f"con_{uuid4().hex}",
            account_id,
            row["consent_version"],
            row["registration_intent_id"],
            hashlib.sha256(payload.encode("utf-8")).hexdigest(),
            _db_time(now),
        ),
    )


def _record_invitation_binding(
    connection: sqlite3.Connection,
    *,
    account_id: str,
    row: sqlite3.Row,
    email: str,
    now: datetime,
) -> bool:
    if row["invitation_id"] is None:
        return False
    invitation = connection.execute(
        """
        SELECT invitation_id,tenant_id,email_fingerprint FROM invitations
        WHERE invitation_id=? AND status='pending' AND email=? AND expires_at>?
        """,
        (row["invitation_id"], email, _db_time(now)),
    ).fetchone()
    if invitation is None:
        return False
    existing = connection.execute(
        """
        SELECT 1 FROM invitation_identity_bindings
        WHERE invitation_id=? OR registration_intent_id=?
        """,
        (invitation["invitation_id"], row["registration_intent_id"]),
    ).fetchone()
    if existing is not None:
        return False
    inserted = connection.execute(
        """
        INSERT INTO invitation_identity_bindings(
          invitation_identity_binding_id,invitation_id,tenant_id,account_id,
          registration_intent_id,email_fingerprint,status,revision,verified_at)
        VALUES(?,?,?,?,?,?,'verified_pending_membership',1,?)
        """,
        (
            f"iib_{uuid4().hex}",
            invitation["invitation_id"],
            invitation["tenant_id"],
            account_id,
            row["registration_intent_id"],
            invitation["email_fingerprint"],
            _db_time(now),
        ),
    ).rowcount
    return inserted == 1


def _write_lifecycle_event(
    connection: sqlite3.Connection,
    *,
    account_id: str | None,
    source_intent_id: str,
    event_type: str,
    reason_code: str,
    request_id: str,
    trace_id: str,
    payload: dict[str, object],
    now: datetime,
) -> None:
    event_id = f"ile_{uuid4().hex}"
    payload_json = _json_text(payload)
    connection.execute(
        """
        INSERT INTO identity_lifecycle_events(
          identity_lifecycle_event_id,account_id,source_intent_id,event_type,reason_code,
          request_id,trace_id,payload_json,payload_sha256,occurred_at)
        VALUES(?,?,?,?,?,?,?,?,?,?)
        """,
        (
            event_id,
            account_id,
            source_intent_id,
            event_type,
            reason_code,
            request_id,
            trace_id,
            payload_json,
            hashlib.sha256(payload_json.encode("utf-8")).hexdigest(),
            _db_time(now),
        ),
    )
    connection.execute(
        """
        INSERT INTO identity_lifecycle_outbox(
          identity_lifecycle_outbox_id,identity_lifecycle_event_id,status,attempt_count,
          generation,next_attempt_at,delivered_at,created_at,updated_at)
        VALUES(?,?,'pending',0,0,?,NULL,?,?)
        """,
        (
            f"ilo_{uuid4().hex}",
            event_id,
            _db_time(now),
            _db_time(now),
            _db_time(now),
        ),
    )


def _account_candidates(connection: sqlite3.Connection, email: str) -> list[sqlite3.Row]:
    return connection.execute(
        """
        SELECT a.account_id,a.status,a.login_enabled,a.credential_revision,a.locked_until,
          c.secret_hash
        FROM global_accounts a
        JOIN account_credentials c ON c.account_id=a.account_id
          AND c.credential_type='password' AND c.status='active'
        WHERE a.email_normalized=?
        ORDER BY a.login_enabled DESC,a.account_id
        """,
        (email,),
    ).fetchall()


def _registration_proof_candidate(
    candidates: list[sqlite3.Row],
    now: datetime,
) -> sqlite3.Row | None:
    if len(candidates) != 1:
        return None
    candidate = candidates[0]
    if candidate["login_enabled"] != 1:
        return None
    if candidate["status"] == "active":
        return candidate
    if candidate["status"] == "locked" and candidate["locked_until"]:
        return candidate if _parse_time(candidate["locked_until"]) <= now else None
    return None


def _reset_candidate(candidates: list[sqlite3.Row]) -> sqlite3.Row | None:
    if len(candidates) != 1:
        return None
    candidate = candidates[0]
    if candidate["login_enabled"] != 1 or candidate["status"] not in {"active", "locked"}:
        return None
    return candidate


def _available_invitation(
    connection: sqlite3.Connection,
    token: str | None,
    email: str,
    now: datetime,
) -> sqlite3.Row | None:
    if type(token) is not str or _INVITATION_TOKEN_RE.fullmatch(token) is None:
        return None
    return connection.execute(
        """
        SELECT invitation_id,tenant_id,email_fingerprint FROM invitations
        WHERE token_hash=? AND email=? AND status='pending' AND expires_at>?
        """,
        (hash_token(token), email, _db_time(now)),
    ).fetchone()


def _supersede_registration_intents(
    connection: sqlite3.Connection,
    email_hmac: str,
    now: datetime,
) -> None:
    connection.execute(
        """
        UPDATE verification_challenges SET status='superseded',updated_at=?
        WHERE status='pending' AND registration_intent_id IN(
          SELECT registration_intent_id FROM registration_intents
          WHERE email_hmac=? AND status='pending')
        """,
        (_db_time(now), email_hmac),
    )
    connection.execute(
        """
        UPDATE registration_intents SET status='superseded',updated_at=?
        WHERE email_hmac=? AND status='pending'
        """,
        (_db_time(now), email_hmac),
    )


def _registration_email(config: AppConfig, row: sqlite3.Row) -> str:
    try:
        decrypted = decrypt_identity_pii(
            row["email_ciphertext"],
            secret=config.identity_pii_encryption_secret,
            owner=_registration_email_owner(str(row["registration_intent_id"])),
        )
    except (IdentityPiiCryptoError, IndexError, KeyError, TypeError):
        raise ApiError("identity.unavailable") from None
    normalized = normalize_global_email(decrypted)
    if normalized is None or not hmac.compare_digest(normalized, decrypted):
        raise ApiError("identity.unavailable")
    return normalized


def _registration_email_owner(intent_id: str) -> IdentityPiiOwner:
    return IdentityPiiOwner(
        purpose="registration",
        intent_id=intent_id,
        field_kind="email_normalized",
        key_version=1,
    )


def _supersede_reset_intents(
    connection: sqlite3.Connection,
    email_hmac: str,
    now: datetime,
) -> None:
    connection.execute(
        """
        UPDATE verification_challenges SET status='superseded',updated_at=?
        WHERE status='pending' AND password_reset_intent_id IN(
          SELECT password_reset_intent_id FROM password_reset_intents
          WHERE email_hmac=? AND status='pending')
        """,
        (_db_time(now), email_hmac),
    )
    connection.execute(
        """
        UPDATE password_reset_intents SET status='expired',updated_at=?
        WHERE email_hmac=? AND status='pending'
        """,
        (_db_time(now), email_hmac),
    )


def _expire_registration_intent(
    connection: sqlite3.Connection,
    intent_id: str,
    now: datetime,
) -> None:
    connection.execute(
        """
        UPDATE verification_challenges SET status='expired',updated_at=?
        WHERE registration_intent_id=? AND status='pending'
        """,
        (_db_time(now), intent_id),
    )
    connection.execute(
        """
        UPDATE registration_intents SET status='expired',updated_at=?
        WHERE registration_intent_id=? AND status='pending'
        """,
        (_db_time(now), intent_id),
    )


def _normalize_display_name(value: str) -> str:
    if type(value) is not str:
        raise ApiError("identity.registration_invalid")
    normalized = unicodedata.normalize("NFKC", value.strip())
    if (
        not normalized
        or len(normalized) > 120
        or any(unicodedata.category(character).startswith("C") for character in normalized)
    ):
        raise ApiError("identity.registration_invalid")
    return normalized


def _normalize_consent_version(value: str) -> str:
    if type(value) is not str:
        raise ApiError("identity.registration_invalid")
    normalized = value.strip()
    if (
        not normalized
        or len(normalized) > 80
        or any(
            character.isspace() or ord(character) < 33 or ord(character) > 126
            for character in normalized
        )
    ):
        raise ApiError("identity.registration_invalid")
    return normalized


def _require_intent_id(value: str, *, purpose: str) -> None:
    pattern = _REGISTRATION_INTENT_RE if purpose == "registration" else _RESET_INTENT_RE
    if type(value) is not str or pattern.fullmatch(value) is None:
        raise ApiError("identity.verification_invalid")


def _require_code(value: str) -> None:
    if type(value) is not str or _CODE_RE.fullmatch(value) is None:
        raise ApiError("identity.verification_invalid")


def _secret_hmac(config: AppConfig, domain: str, value: str) -> str:
    return hmac.new(
        config.identity_challenge_secret.encode("utf-8"),
        f"metahub:identity-challenge:{domain}:".encode("ascii") + value.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _code_hmac(
    config: AppConfig,
    purpose: str,
    intent_id: str,
    generation: int,
    code: str,
) -> str:
    return _secret_hmac(config, "code", f"{purpose}\x1f{intent_id}\x1f{generation}\x1f{code}")


def _client_key(client_host: str | None, user_agent: str | None) -> str:
    return f"{(client_host or '')[:64]}\x1f{(user_agent or '')[:256]}"


def _connect(config: AppConfig) -> sqlite3.Connection:
    try:
        return connect_database(config.database_url)
    except (DbError, OSError):
        raise ApiError("identity.unavailable") from None


def _json_text(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _db_time(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
