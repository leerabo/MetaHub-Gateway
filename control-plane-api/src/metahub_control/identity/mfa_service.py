"""PRD-095 TOTP enrollment and atomic counter consumption."""

from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from metahub_shared.db.connection import connect_database
from metahub_shared.db.security import verify_password
from metahub_shared.identity import (
    MfaCryptoError,
    MfaSecretOwner,
    decrypt_mfa_secret,
    encrypt_mfa_secret,
    generate_totp_secret,
    totp_counter,
)
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError

from .service import authenticate_global_session_in_transaction


@dataclass(frozen=True, slots=True, repr=False)
class PendingMfaEnrollment:
    factor_id: str
    secret: str
    uri: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class VerifiedMfaFactor:
    factor_id: str
    revision: int
    verified_at: datetime


def begin_totp_enrollment(
    config: AppConfig,
    *,
    session_token: str,
    csrf_token: str,
    current_password: str,
    request_id: str,
    trace_id: str,
) -> PendingMfaEnrollment:
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        _require_capability(connection)
        identity, now = authenticate_global_session_in_transaction(
            connection,
            config,
            session_token=session_token,
            csrf_token=csrf_token,
        )
        credential = connection.execute(
            """
            SELECT secret_hash FROM account_credentials
            WHERE account_id=? AND credential_type='password' AND status='active'
            """,
            (identity.account_id,),
        ).fetchone()
        if credential is None or not verify_password(current_password, credential["secret_hash"]):
            raise ApiError("identity.reauthentication_failed")
        if (
            _column_exists(connection, "global_sessions", "assurance_level")
            and identity.current_session.assurance_level != "mfa"
            and connection.execute(
                """SELECT 1 FROM account_mfa_factors
                     WHERE account_id=? AND status='active' LIMIT 1""",
                (identity.account_id,),
            ).fetchone()
            is not None
        ):
            raise ApiError("identity.mfa_required")
        connection.execute(
            """
            UPDATE account_mfa_factors
            SET status='disabled',disabled_at=?,updated_at=?
            WHERE account_id=? AND status='pending'
            """,
            (_db_time(now), _db_time(now), identity.account_id),
        )
        factor_id = _id("mfa_")
        plaintext = generate_totp_secret()
        ciphertext = encrypt_mfa_secret(
            plaintext,
            secret=config.identity_mfa_encryption_secret,
            owner=MfaSecretOwner(identity.account_id, factor_id),
        )
        connection.execute(
            """
            INSERT INTO account_mfa_factors(
              factor_id,account_id,factor_type,secret_ciphertext,secret_version,status,
              revision,last_accepted_counter,verified_at,disabled_at,created_at,updated_at)
            VALUES(?,?,'totp',?,1,'pending',1,NULL,NULL,NULL,?,?)
            """,
            (factor_id, identity.account_id, ciphertext, _db_time(now), _db_time(now)),
        )
        _audit(
            connection,
            actor_domain="global",
            actor_id=identity.account_id,
            action="identity.mfa_enrollment_started",
            object_type="mfa_factor",
            object_id=factor_id,
            reason="user_requested",
            request_id=request_id,
            trace_id=trace_id,
            now=now,
        )
        _write_prd116_event_if_available(
            connection,
            account_id=identity.account_id,
            factor_id=factor_id,
            reason="totp_enrollment_started",
            status="pending",
            request_id=request_id,
            trace_id=trace_id,
            now=now,
        )
        connection.commit()
        return PendingMfaEnrollment(
            factor_id=factor_id,
            secret=plaintext,
            uri=(
                f"otpauth://totp/MetaHub:{factor_id}?secret={plaintext}"
                "&issuer=MetaHub&algorithm=SHA1&digits=6&period=30"
            ),
            expires_at=now + timedelta(minutes=10),
        )
    except ApiError:
        if connection.in_transaction:
            connection.rollback()
        raise
    except (sqlite3.Error, MfaCryptoError, ValueError, TypeError):
        if connection.in_transaction:
            connection.rollback()
        raise ApiError("identity.mfa_unavailable") from None
    finally:
        connection.close()


def verify_totp_enrollment(
    config: AppConfig,
    *,
    session_token: str,
    csrf_token: str,
    factor_id: str,
    code: str,
    device_fingerprint: str,
    request_id: str,
    trace_id: str,
) -> VerifiedMfaFactor:
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        identity, now = authenticate_global_session_in_transaction(
            connection,
            config,
            session_token=session_token,
            csrf_token=csrf_token,
        )
        row = connection.execute(
            """
            SELECT * FROM account_mfa_factors
            WHERE factor_id=? AND account_id=? AND status='pending'
            """,
            (factor_id, identity.account_id),
        ).fetchone()
        if row is None or _parse_time(row["created_at"]) < now - timedelta(minutes=10):
            raise ApiError("identity.mfa_verification_failed")
        _require_not_locked(
            connection,
            config,
            identity.account_id,
            factor_id,
            device_fingerprint,
            now,
        )
        counter = _matching_counter(config, row, code, now)
        if counter is None:
            _record_failure(
                connection,
                config,
                identity.account_id,
                factor_id,
                device_fingerprint,
                now,
            )
            connection.commit()
            raise ApiError("identity.mfa_verification_failed")
        connection.execute(
            """
            UPDATE account_mfa_factors
            SET status='active',revision=revision+1,last_accepted_counter=?,
              verified_at=?,updated_at=?
            WHERE factor_id=? AND account_id=? AND status='pending'
            """,
            (counter, _db_time(now), _db_time(now), factor_id, identity.account_id),
        )
        if _column_exists(connection, "global_sessions", "assurance_level"):
            connection.execute(
                """UPDATE global_sessions
                      SET assurance_level='mfa',auth_method='totp',updated_at=?
                    WHERE session_id=? AND account_id=? AND status='active'""",
                (_db_time(now), identity.session_id, identity.account_id),
            )
        _clear_failures(connection, identity.account_id, factor_id)
        _audit(
            connection,
            actor_domain="global",
            actor_id=identity.account_id,
            action="identity.mfa_factor_activated",
            object_type="mfa_factor",
            object_id=factor_id,
            reason="totp_verified",
            request_id=request_id,
            trace_id=trace_id,
            now=now,
        )
        _write_prd116_event_if_available(
            connection,
            account_id=identity.account_id,
            factor_id=factor_id,
            reason="totp_activated",
            status="active",
            request_id=request_id,
            trace_id=trace_id,
            now=now,
        )
        revision = int(row["revision"]) + 1
        connection.commit()
        return VerifiedMfaFactor(factor_id=factor_id, revision=revision, verified_at=now)
    except ApiError:
        if connection.in_transaction:
            connection.rollback()
        raise
    except (sqlite3.Error, MfaCryptoError, ValueError, TypeError):
        if connection.in_transaction:
            connection.rollback()
        raise ApiError("identity.mfa_unavailable") from None
    finally:
        connection.close()


def consume_active_totp_in_transaction(
    connection: sqlite3.Connection,
    config: AppConfig,
    *,
    account_id: str,
    code: str,
    device_fingerprint: str,
    now: datetime,
) -> tuple[str, int, int]:
    """Consume one strictly increasing counter and return factor identity and revision."""

    _require_capability(connection)
    row = connection.execute(
        """SELECT * FROM account_mfa_factors
             WHERE account_id=? AND factor_type='totp' AND status='active'""",
        (account_id,),
    ).fetchone()
    if row is None:
        raise ApiError("identity.mfa_required")
    _require_not_locked(connection, config, account_id, row["factor_id"], device_fingerprint, now)
    counter = _matching_counter(config, row, code, now)
    if counter is None or counter <= int(row["last_accepted_counter"]):
        _record_failure(connection, config, account_id, row["factor_id"], device_fingerprint, now)
        raise ApiError("identity.mfa_verification_failed")
    updated = connection.execute(
        """
        UPDATE account_mfa_factors SET last_accepted_counter=?,updated_at=?
        WHERE factor_id=? AND account_id=? AND status='active' AND last_accepted_counter<?
        """,
        (counter, _db_time(now), row["factor_id"], account_id, counter),
    ).rowcount
    if updated != 1:
        raise ApiError("identity.mfa_verification_failed")
    _clear_failures(connection, account_id, row["factor_id"])
    return str(row["factor_id"]), int(row["revision"]), counter


def _matching_counter(config: AppConfig, row: sqlite3.Row, code: str, now: datetime) -> int | None:
    plaintext = decrypt_mfa_secret(
        row["secret_ciphertext"],
        secret=config.identity_mfa_encryption_secret,
        owner=MfaSecretOwner(row["account_id"], row["factor_id"], row["secret_version"]),
    )
    return totp_counter(code, plaintext, unix_time=int(now.timestamp()))


def _record_failure(
    connection: sqlite3.Connection,
    config: AppConfig,
    account_id: str,
    factor_id: str,
    device_fingerprint: str,
    now: datetime,
) -> None:
    device_hmac = _device_hmac(config, device_fingerprint)
    connection.execute(
        """
        INSERT INTO account_mfa_attempt_guards(
          account_id,factor_id,device_hmac,failed_count,locked_until,updated_at)
        VALUES(?,?,?,1,NULL,?)
        ON CONFLICT(account_id,factor_id,device_hmac) DO UPDATE SET
          failed_count=MIN(account_mfa_attempt_guards.failed_count+1,20),
          locked_until=CASE WHEN account_mfa_attempt_guards.failed_count+1>=5
            THEN datetime(?, '+5 minutes') ELSE account_mfa_attempt_guards.locked_until END,
          updated_at=excluded.updated_at
        """,
        (account_id, factor_id, device_hmac, _db_time(now), _db_time(now)),
    )


def _require_not_locked(
    connection: sqlite3.Connection,
    config: AppConfig,
    account_id: str,
    factor_id: str,
    device_fingerprint: str,
    now: datetime,
) -> None:
    row = connection.execute(
        """
        SELECT locked_until FROM account_mfa_attempt_guards
        WHERE account_id=? AND factor_id=? AND device_hmac=?
        """,
        (account_id, factor_id, _device_hmac(config, device_fingerprint)),
    ).fetchone()
    if (
        row is not None
        and row["locked_until"] is not None
        and _parse_time(row["locked_until"]) > now
    ):
        raise ApiError("identity.mfa_verification_failed")


def _clear_failures(connection: sqlite3.Connection, account_id: str, factor_id: str) -> None:
    connection.execute(
        "DELETE FROM account_mfa_attempt_guards WHERE account_id=? AND factor_id=?",
        (account_id, factor_id),
    )


def _require_capability(connection: sqlite3.Connection) -> None:
    if (
        connection.execute(
            "SELECT 1 FROM sqlite_schema WHERE type='table' AND name='account_mfa_factors'"
        ).fetchone()
        is None
    ):
        raise ApiError("identity.mfa_unavailable")


def _column_exists(connection: sqlite3.Connection, table: str, column: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM pragma_table_info(?) WHERE name=?",
        (table, column),
    ).fetchone() is not None


def _write_prd116_event_if_available(
    connection: sqlite3.Connection,
    *,
    account_id: str,
    factor_id: str,
    reason: str,
    status: str,
    request_id: str,
    trace_id: str,
    now: datetime,
) -> None:
    if connection.execute(
        "SELECT 1 FROM sqlite_schema WHERE type='table' AND name='identity_security_events'"
    ).fetchone() is None:
        return
    from .security_service import _write_security_event

    _write_security_event(
        connection,
        account_id=account_id,
        actor_account_id=account_id,
        event_type="identity.mfa_changed",
        subject_type="factor",
        subject_id=factor_id,
        reason_code=reason,
        request_id=request_id,
        trace_id=trace_id,
        payload={"schema_version": 1, "factor_type": "totp", "status": status},
        now=now,
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
) -> None:
    digest = hashlib.sha256(
        json.dumps([action, object_type, object_id], separators=(",", ":")).encode("ascii")
    ).hexdigest()
    connection.execute(
        """
        INSERT INTO platform_audit_logs(
          audit_id,actor_domain,actor_id,action_code,object_type,object_id,reason_code,
          request_id,trace_id,before_sha256,after_sha256,occurred_at)
        VALUES(?,?,?,?,?,?,?,?,?,NULL,?,?)
        """,
        (
            _id("pau_"),
            actor_domain,
            actor_id,
            action,
            object_type,
            object_id,
            reason,
            request_id,
            trace_id,
            digest,
            _db_time(now),
        ),
    )


def _device_hmac(config: AppConfig, value: str) -> str:
    return hmac.new(
        config.identity_session_secret.encode("utf-8"),
        b"prd095/mfa/device\x00" + value.encode("utf-8", errors="replace")[:512],
        hashlib.sha256,
    ).hexdigest()


def _connect(config: AppConfig) -> sqlite3.Connection:
    try:
        return connect_database(config.database_url)
    except Exception:
        raise ApiError("identity.mfa_unavailable") from None


def _id(prefix: str) -> str:
    return prefix + uuid4().hex


def _db_time(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
