"""Authoritative global account credentials and opaque browser sessions."""

from __future__ import annotations

import hashlib
import hmac
import json
import random
import re
import secrets
import sqlite3
import time
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from metahub_shared.db.connection import connect_database
from metahub_shared.db.errors import DbError
from metahub_shared.db.security import hash_password, hash_password_argon2id, verify_password
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError

from .models import GlobalAccountSummary, GlobalSessionSummary

SESSION_COOKIE = "__Host-metahub-global-session"
CSRF_COOKIE = "__Host-metahub-global-csrf"
_OPAQUE_RE = re.compile(r"^[A-Za-z0-9_-]{43,128}$")
_SESSION_ID_RE = re.compile(r"^gss_[0-9a-f]{32}$")
_DUMMY_PASSWORD_HASH = hash_password(
    "MetaHub identity dummy verifier value",
    salt=b"metahub-id-dummy",
)
# Keep PBKDF2 legacy, Argon2id, unknown, disabled, locked, and malformed-hash failures inside
# the same wall-clock envelope on the supported local hardware profile.
_MIN_FAILURE_SECONDS = 1.25


@dataclass(frozen=True, slots=True)
class IssuedIdentitySession:
    account: GlobalAccountSummary
    current_session: GlobalSessionSummary
    session_token: str
    csrf_token: str


@dataclass(frozen=True, slots=True)
class AuthenticatedIdentity:
    account_id: str
    session_id: str
    account: GlobalAccountSummary
    current_session: GlobalSessionSummary


def login_global_account(
    config: AppConfig,
    *,
    email: str,
    password: str,
    request_id: str,
    trace_id: str,
    user_agent: str | None,
    client_host: str | None,
) -> IssuedIdentitySession:
    started = time.monotonic()
    normalized_email = _normalize_email(email)
    connection = _connect(config)
    try:
        candidate = _account_by_email(connection, normalized_email) if normalized_email else None
        candidate_hash = candidate["secret_hash"] if candidate is not None else _DUMMY_PASSWORD_HASH
        password_matches = verify_password(password, candidate_hash)

        connection.execute("BEGIN IMMEDIATE")
        account = _account_by_email(connection, normalized_email) if normalized_email else None
        if account is not None and account["secret_hash"] != candidate_hash:
            password_matches = verify_password(password, account["secret_hash"])

        now = _utcnow()
        valid_state = _account_accepts_login(account, now)
        if not account or not valid_state or not password_matches:
            account_id = account["account_id"] if account is not None else None
            if account is not None and valid_state and not password_matches:
                _record_failed_login(connection, account, config, now)
            _write_event(
                connection,
                account_id=account_id,
                session_id=None,
                event_type="identity.login_failed",
                reason_code="invalid_identity",
                request_id=request_id,
                trace_id=trace_id,
                payload={"schema_version": 1, "reason_code": "invalid_identity"},
                now=now,
            )
            connection.commit()
            _pad_failed_login(started)
            raise ApiError("identity.login_invalid")

        if account["algorithm"] == "pbkdf2_sha256_legacy":
            upgraded_hash = hash_password_argon2id(password)
            _replace_credential_hash(connection, account["account_id"], upgraded_hash, now)

        connection.execute(
            """
            UPDATE global_accounts
            SET status='active',failed_login_count=0,locked_until=NULL,last_login_at=?,updated_at=?
            WHERE account_id=?
            """,
            (_db_time(now), _db_time(now), account["account_id"]),
        )
        account = _account_by_id(connection, account["account_id"])
        issued = _create_session(
            connection,
            config,
            account,
            now=now,
            user_agent=user_agent,
            client_host=client_host,
        )
        _write_event(
            connection,
            account_id=account["account_id"],
            session_id=issued.current_session.session_id,
            event_type="identity.login_succeeded",
            reason_code="password_verified",
            request_id=request_id,
            trace_id=trace_id,
            payload={
                "schema_version": 1,
                "credential_revision": account["credential_revision"],
                "session_revision": account["session_revision"],
            },
            now=now,
        )
        connection.commit()
        return issued
    except ApiError:
        if connection.in_transaction:
            connection.rollback()
        raise
    except (sqlite3.Error, DbError, ValueError, TypeError):
        if connection.in_transaction:
            connection.rollback()
        raise ApiError("identity.unavailable") from None
    finally:
        connection.close()


def authenticate_global_session(
    config: AppConfig,
    session_token: str,
) -> AuthenticatedIdentity:
    connection = _connect(config)
    try:
        now = _utcnow()
        identity = _authenticate(connection, config, session_token, now, touch=True)
        connection.commit()
        return identity
    except ApiError as exc:
        if connection.in_transaction:
            if exc.code == "identity.session_expired":
                connection.commit()
            else:
                connection.rollback()
        raise
    except (sqlite3.Error, DbError, ValueError, TypeError):
        if connection.in_transaction:
            connection.rollback()
        raise ApiError("identity.unavailable") from None
    finally:
        connection.close()


def list_global_sessions(
    config: AppConfig,
    *,
    session_token: str,
    limit: int,
    offset: int,
) -> tuple[AuthenticatedIdentity, tuple[GlobalSessionSummary, ...], int]:
    connection = _connect(config)
    try:
        now = _utcnow()
        identity = _authenticate(connection, config, session_token, now, touch=True)
        total = int(
            connection.execute(
                """
                SELECT COUNT(*) FROM global_sessions
                WHERE account_id=? AND status='active' AND expires_at>? AND idle_expires_at>?
                """,
                (identity.account_id, _db_time(now), _db_time(now)),
            ).fetchone()[0]
        )
        rows = connection.execute(
            """
            SELECT session_id,device_family,created_at,
                   COALESCE(last_seen_at,created_at) last_seen_at,expires_at
            FROM global_sessions
            WHERE account_id=? AND status='active' AND expires_at>? AND idle_expires_at>?
            ORDER BY CASE WHEN session_id=? THEN 0 ELSE 1 END,created_at DESC,session_id
            LIMIT ? OFFSET ?
            """,
            (
                identity.account_id,
                _db_time(now),
                _db_time(now),
                identity.session_id,
                limit,
                offset,
            ),
        ).fetchall()
        sessions = tuple(
            _session_summary(row, current_session_id=identity.session_id) for row in rows
        )
        connection.commit()
        return identity, sessions, total
    except ApiError as exc:
        if connection.in_transaction:
            if exc.code == "identity.session_expired":
                connection.commit()
            else:
                connection.rollback()
        raise
    except (sqlite3.Error, DbError, ValueError, TypeError):
        if connection.in_transaction:
            connection.rollback()
        raise ApiError("identity.unavailable") from None
    finally:
        connection.close()


def revoke_global_session(
    config: AppConfig,
    *,
    session_token: str,
    csrf_token: str,
    target_session_id: str,
    reason: str,
    request_id: str,
    trace_id: str,
) -> tuple[AuthenticatedIdentity, bool]:
    if not _SESSION_ID_RE.fullmatch(target_session_id):
        raise ApiError("identity.session_not_found")
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        now = _utcnow()
        identity = _authenticate(connection, config, session_token, now, touch=False)
        _verify_csrf(connection, config, identity.session_id, csrf_token)
        target = connection.execute(
            "SELECT account_id,status FROM global_sessions WHERE session_id=?",
            (target_session_id,),
        ).fetchone()
        if target is None or target["account_id"] != identity.account_id:
            raise ApiError("identity.session_not_found")
        if target["status"] == "active":
            connection.execute(
                """
                UPDATE global_sessions
                SET status='revoked',revoked_at=?,revoked_reason=?,updated_at=?
                WHERE session_id=? AND account_id=? AND status='active'
                """,
                (
                    _db_time(now),
                    reason,
                    _db_time(now),
                    target_session_id,
                    identity.account_id,
                ),
            )
            _write_event(
                connection,
                account_id=identity.account_id,
                session_id=target_session_id,
                event_type="identity.session_revoked",
                reason_code=reason,
                request_id=request_id,
                trace_id=trace_id,
                payload={"schema_version": 1, "reason_code": reason},
                now=now,
            )
        connection.commit()
        return identity, target_session_id == identity.session_id
    except ApiError as exc:
        if connection.in_transaction:
            if exc.code == "identity.session_expired":
                connection.commit()
            else:
                connection.rollback()
        raise
    except (sqlite3.Error, DbError, ValueError, TypeError):
        if connection.in_transaction:
            connection.rollback()
        raise ApiError("identity.unavailable") from None
    finally:
        connection.close()


def rotate_global_password(
    config: AppConfig,
    *,
    session_token: str,
    csrf_token: str,
    current_password: str,
    new_password: str,
    request_id: str,
    trace_id: str,
    user_agent: str | None,
    client_host: str | None,
) -> IssuedIdentitySession:
    _validate_password(new_password)
    if hmac.compare_digest(current_password, new_password):
        raise ApiError("identity.password_invalid")
    connection = _connect(config)
    try:
        now = _utcnow()
        identity = _authenticate(connection, config, session_token, now, touch=False)
        _verify_csrf(connection, config, identity.session_id, csrf_token)
        credential = connection.execute(
            """
            SELECT secret_hash FROM account_credentials
            WHERE account_id=? AND credential_type='password' AND status='active'
            """,
            (identity.account_id,),
        ).fetchone()
        if credential is None or not verify_password(current_password, credential["secret_hash"]):
            raise ApiError("identity.login_invalid")

        # Argon2 is deliberately deferred until the session, CSRF binding, and current credential
        # are authoritative. The compare below closes the race before the replacement transaction.
        expected_hash = str(credential["secret_hash"])
        new_hash = hash_password_argon2id(new_password)
        connection.execute("BEGIN IMMEDIATE")
        now = _utcnow()
        identity = _authenticate(connection, config, session_token, now, touch=False)
        _verify_csrf(connection, config, identity.session_id, csrf_token)
        current = connection.execute(
            """
            SELECT secret_hash FROM account_credentials
            WHERE account_id=? AND credential_type='password' AND status='active'
            """,
            (identity.account_id,),
        ).fetchone()
        if current is None or not hmac.compare_digest(str(current["secret_hash"]), expected_hash):
            raise ApiError("identity.session_revoked")
        _replace_credential_hash(connection, identity.account_id, new_hash, now)
        account = _account_by_id(connection, identity.account_id)
        issued = _create_session(
            connection,
            config,
            account,
            now=now,
            user_agent=user_agent,
            client_host=client_host,
        )
        _write_event(
            connection,
            account_id=identity.account_id,
            session_id=issued.current_session.session_id,
            event_type="identity.credential_rotated",
            reason_code="user_requested",
            request_id=request_id,
            trace_id=trace_id,
            payload={
                "schema_version": 1,
                "credential_revision": account["credential_revision"],
                "session_revision": account["session_revision"],
            },
            now=now,
        )
        connection.commit()
        return issued
    except ApiError as exc:
        if connection.in_transaction:
            if exc.code == "identity.session_expired":
                connection.commit()
            else:
                connection.rollback()
        raise
    except (sqlite3.Error, DbError, ValueError, TypeError):
        if connection.in_transaction:
            connection.rollback()
        raise ApiError("identity.unavailable") from None
    finally:
        connection.close()


def valid_opaque_value(value: str) -> bool:
    return bool(_OPAQUE_RE.fullmatch(value))


def issue_global_session_in_transaction(
    connection: sqlite3.Connection,
    config: AppConfig,
    *,
    account_id: str,
    now: datetime,
    user_agent: str | None,
    client_host: str | None,
) -> IssuedIdentitySession:
    """Issue a PRD-091 GlobalSession inside an existing identity transaction."""

    account = _account_by_id(connection, account_id)
    return _create_session(
        connection,
        config,
        account,
        now=now,
        user_agent=user_agent,
        client_host=client_host,
    )


def replace_global_password_in_transaction(
    connection: sqlite3.Connection,
    *,
    account_id: str,
    new_hash: str,
    now: datetime,
) -> None:
    """Rotate a credential and revoke prior sessions using the PRD-091 projection rules."""

    _replace_credential_hash(connection, account_id, new_hash, now)


def normalize_global_email(value: str) -> str | None:
    return _normalize_email(value)


def validate_global_password(value: str) -> None:
    _validate_password(value)


def _connect(config: AppConfig) -> sqlite3.Connection:
    try:
        return connect_database(config.database_url)
    except DbError:
        raise ApiError("identity.unavailable") from None


def _account_by_email(connection: sqlite3.Connection, email: str) -> sqlite3.Row | None:
    return connection.execute(
        """
        SELECT a.*,c.secret_hash,c.algorithm,c.version credential_version
        FROM global_accounts a
        JOIN account_credentials c ON c.account_id=a.account_id
          AND c.credential_type='password' AND c.status='active'
        WHERE a.email_normalized=? AND a.login_enabled=1
        """,
        (email,),
    ).fetchone()


def _account_by_id(connection: sqlite3.Connection, account_id: str) -> sqlite3.Row:
    row = connection.execute(
        """
        SELECT a.*,c.secret_hash,c.algorithm,c.version credential_version
        FROM global_accounts a
        JOIN account_credentials c ON c.account_id=a.account_id
          AND c.credential_type='password' AND c.status='active'
        WHERE a.account_id=?
        """,
        (account_id,),
    ).fetchone()
    if row is None:
        raise ApiError("identity.session_invalid")
    return row


def _account_accepts_login(account: sqlite3.Row | None, now: datetime) -> bool:
    if account is None or account["login_enabled"] != 1:
        return False
    if account["status"] == "active":
        return True
    if account["status"] == "locked" and account["locked_until"]:
        return _parse_time(account["locked_until"]) <= now
    return False


def _record_failed_login(
    connection: sqlite3.Connection,
    account: sqlite3.Row,
    config: AppConfig,
    now: datetime,
) -> None:
    failures = int(account["failed_login_count"]) + 1
    if failures >= config.login_max_failures:
        locked_until = now + timedelta(seconds=config.login_lock_seconds)
        connection.execute(
            """
            UPDATE global_accounts
            SET failed_login_count=?,status='locked',locked_until=?,updated_at=?
            WHERE account_id=?
            """,
            (failures, _db_time(locked_until), _db_time(now), account["account_id"]),
        )
    else:
        connection.execute(
            "UPDATE global_accounts SET failed_login_count=?,updated_at=? WHERE account_id=?",
            (failures, _db_time(now), account["account_id"]),
        )


def _replace_credential_hash(
    connection: sqlite3.Connection,
    account_id: str,
    new_hash: str,
    now: datetime,
) -> None:
    links = connection.execute(
        """
        SELECT legacy_tenant_id,legacy_user_id FROM account_identity_links
        WHERE account_id=? AND source_type='legacy_user' AND status='active'
        """,
        (account_id,),
    ).fetchall()
    if links:
        for link in links:
            updated = connection.execute(
                """
                UPDATE users SET password_hash=?,updated_at=?
                WHERE tenant_id=? AND user_id=?
                """,
                (
                    new_hash,
                    _db_time(now),
                    link["legacy_tenant_id"],
                    link["legacy_user_id"],
                ),
            ).rowcount
            if updated != 1:
                raise sqlite3.IntegrityError("identity_legacy_projection_missing")
        return
    connection.execute(
        """
        UPDATE account_credentials
        SET secret_hash=?,algorithm='argon2id',version=version+1,updated_at=?,rotated_at=?
        WHERE account_id=? AND credential_type='password' AND status='active'
        """,
        (new_hash, _db_time(now), _db_time(now), account_id),
    )
    connection.execute(
        """
        UPDATE global_accounts
        SET credential_revision=credential_revision+1,session_revision=session_revision+1,
            failed_login_count=0,locked_until=NULL,updated_at=?
        WHERE account_id=?
        """,
        (_db_time(now), account_id),
    )
    connection.execute(
        """
        UPDATE global_sessions
        SET status='revoked',revoked_at=?,revoked_reason='credential_rotated',updated_at=?
        WHERE account_id=? AND status='active'
        """,
        (_db_time(now), _db_time(now), account_id),
    )


def _create_session(
    connection: sqlite3.Connection,
    config: AppConfig,
    account: sqlite3.Row,
    *,
    now: datetime,
    user_agent: str | None,
    client_host: str | None,
) -> IssuedIdentitySession:
    session_id = f"gss_{uuid4().hex}"
    session_token = secrets.token_urlsafe(32)
    csrf_token = secrets.token_urlsafe(32)
    expires_at = now + timedelta(seconds=config.session_ttl_seconds)
    device_family = _device_family(user_agent)
    connection.execute(
        """
        INSERT INTO global_sessions(
          session_id,account_id,token_hmac,csrf_hmac,status,credential_revision,
          session_revision,device_family,client_risk_hmac,expires_at,idle_expires_at,
          last_seen_at,revoked_at,revoked_reason,created_at,updated_at)
        VALUES(?,?,?,?, 'active',?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            session_id,
            account["account_id"],
            _secret_hmac(config, "session", session_token),
            _secret_hmac(config, "csrf", csrf_token),
            account["credential_revision"],
            account["session_revision"],
            device_family,
            _client_risk_hmac(config, user_agent, client_host),
            _db_time(expires_at),
            _db_time(expires_at),
            _db_time(now),
            None,
            None,
            _db_time(now),
            _db_time(now),
        ),
    )
    return IssuedIdentitySession(
        account=_account_summary(account),
        current_session=GlobalSessionSummary(
            session_id=session_id,
            device_family=device_family,
            created_at=now,
            last_seen_at=now,
            expires_at=expires_at,
            is_current=True,
        ),
        session_token=session_token,
        csrf_token=csrf_token,
    )


def _authenticate(
    connection: sqlite3.Connection,
    config: AppConfig,
    session_token: str,
    now: datetime,
    *,
    touch: bool,
) -> AuthenticatedIdentity:
    if not valid_opaque_value(session_token):
        raise ApiError("identity.session_invalid")
    row = connection.execute(
        """
        SELECT s.*,a.email_normalized,a.display_name,a.status account_status,a.login_enabled,
               a.mfa_state,a.credential_revision account_credential_revision,
               a.session_revision account_session_revision
        FROM global_sessions s JOIN global_accounts a ON a.account_id=s.account_id
        WHERE s.token_hmac=?
        """,
        (_secret_hmac(config, "session", session_token),),
    ).fetchone()
    if row is None:
        raise ApiError("identity.session_invalid")
    if row["status"] == "revoked":
        raise ApiError("identity.session_revoked")
    if row["status"] == "expired":
        raise ApiError("identity.session_expired")
    if _parse_time(row["expires_at"]) <= now or _parse_time(row["idle_expires_at"]) <= now:
        connection.execute(
            "UPDATE global_sessions SET status='expired',updated_at=? WHERE session_id=?",
            (_db_time(now), row["session_id"]),
        )
        raise ApiError("identity.session_expired")
    if (
        row["account_status"] != "active"
        or row["login_enabled"] != 1
        or row["credential_revision"] != row["account_credential_revision"]
        or row["session_revision"] != row["account_session_revision"]
    ):
        raise ApiError("identity.session_revoked")
    if touch and _parse_time(row["last_seen_at"] or row["created_at"]) <= now - timedelta(
        seconds=60
    ):
        connection.execute(
            "UPDATE global_sessions SET last_seen_at=?,updated_at=? WHERE session_id=?",
            (_db_time(now), _db_time(now), row["session_id"]),
        )
        row = connection.execute(
            "SELECT * FROM global_sessions WHERE session_id=?",
            (row["session_id"],),
        ).fetchone()
        account = _account_by_id(connection, row["account_id"])
        return AuthenticatedIdentity(
            account_id=row["account_id"],
            session_id=row["session_id"],
            account=_account_summary(account),
            current_session=_session_summary(row, current_session_id=row["session_id"]),
        )
    return AuthenticatedIdentity(
        account_id=row["account_id"],
        session_id=row["session_id"],
        account=GlobalAccountSummary(
            email=row["email_normalized"],
            display_name=row["display_name"],
            status=row["account_status"],
            mfa_configured=row["mfa_state"] != "not_enrolled",
        ),
        current_session=_session_summary(row, current_session_id=row["session_id"]),
    )


def _verify_csrf(
    connection: sqlite3.Connection,
    config: AppConfig,
    session_id: str,
    csrf_token: str,
) -> None:
    if not valid_opaque_value(csrf_token):
        raise ApiError("identity.csrf_invalid")
    row = connection.execute(
        "SELECT csrf_hmac FROM global_sessions WHERE session_id=?",
        (session_id,),
    ).fetchone()
    if row is None or not hmac.compare_digest(
        row["csrf_hmac"], _secret_hmac(config, "csrf", csrf_token)
    ):
        raise ApiError("identity.csrf_invalid")


def _write_event(
    connection: sqlite3.Connection,
    *,
    account_id: str | None,
    session_id: str | None,
    event_type: str,
    reason_code: str,
    request_id: str,
    trace_id: str,
    payload: dict[str, object],
    now: datetime,
) -> None:
    payload_json = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    event_id = f"iev_{uuid4().hex}"
    connection.execute(
        """
        INSERT INTO identity_events(
          identity_event_id,account_id,session_id,event_type,reason_code,request_id,trace_id,
          payload_json,payload_sha256,occurred_at)
        VALUES(?,?,?,?,?,?,?,?,?,?)
        """,
        (
            event_id,
            account_id,
            session_id,
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
        INSERT INTO identity_event_outbox(
          outbox_id,identity_event_id,status,attempt_count,generation,lease_owner_id,
          lease_expires_at,next_attempt_at,last_error_code,delivered_at,created_at,updated_at)
        VALUES(?,?,'pending',0,0,NULL,NULL,?,NULL,NULL,?,?)
        """,
        (f"iob_{uuid4().hex}", event_id, _db_time(now), _db_time(now), _db_time(now)),
    )


def _normalize_email(value: str) -> str | None:
    if type(value) is not str:
        return None
    normalized = unicodedata.normalize("NFKC", value.strip())
    if len(normalized.encode("utf-8")) > 320 or normalized.count("@") != 1:
        return None
    local, domain = normalized.rsplit("@", 1)
    if not local or not domain or any(character.isspace() for character in normalized):
        return None
    try:
        ascii_domain = domain.encode("idna").decode("ascii").lower()
    except (UnicodeError, ValueError):
        return None
    result = f"{local.lower()}@{ascii_domain}"
    return result if 3 <= len(result) <= 320 else None


def _validate_password(password: str) -> None:
    if type(password) is not str:
        raise ApiError("identity.password_invalid")
    try:
        byte_length = len(password.encode("utf-8"))
    except UnicodeEncodeError:
        raise ApiError("identity.password_invalid") from None
    if byte_length < 8 or byte_length > 1024 or "\x00" in password:
        raise ApiError("identity.password_invalid")


def _secret_hmac(config: AppConfig, domain: str, value: str) -> str:
    return hmac.new(
        config.identity_session_secret.encode("utf-8"),
        f"metahub:identity:{domain}:".encode("ascii") + value.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _client_risk_hmac(
    config: AppConfig,
    user_agent: str | None,
    client_host: str | None,
) -> str:
    bounded = f"{(client_host or '')[:64]}\x1f{(user_agent or '')[:256]}"
    return _secret_hmac(config, "client-risk", bounded)


def _device_family(user_agent: str | None) -> str:
    value = (user_agent or "").lower()
    if "edg/" in value:
        return "edge"
    if "firefox/" in value:
        return "firefox"
    if "chrome/" in value or "chromium/" in value:
        return "chrome"
    if "safari/" in value:
        return "safari"
    return "unknown"


def _account_summary(row: sqlite3.Row) -> GlobalAccountSummary:
    return GlobalAccountSummary(
        email=row["email_normalized"],
        display_name=row["display_name"],
        status=row["status"] if "status" in row.keys() else row["account_status"],
        mfa_configured=row["mfa_state"] != "not_enrolled",
    )


def _session_summary(row: sqlite3.Row, *, current_session_id: str) -> GlobalSessionSummary:
    return GlobalSessionSummary(
        session_id=row["session_id"],
        device_family=row["device_family"],
        created_at=_parse_time(row["created_at"]),
        last_seen_at=_parse_time(row["last_seen_at"] or row["created_at"]),
        expires_at=_parse_time(row["expires_at"]),
        is_current=row["session_id"] == current_session_id,
    )


def _pad_failed_login(started: float) -> None:
    target = _MIN_FAILURE_SECONDS + random.SystemRandom().uniform(0.0, 0.01)
    remaining = target - (time.monotonic() - started)
    if remaining > 0:
        time.sleep(remaining)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _db_time(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
