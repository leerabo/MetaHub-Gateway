"""Offline, one-time installation of the first owner on an empty production DB.

This command is deliberately not registered as an HTTP endpoint. Enrollment
material is displayed only on an interactive terminal, never in container logs.
"""
from __future__ import annotations

from datetime import UTC, datetime
import getpass
import json
import os
from pathlib import Path
import sys
from urllib.parse import quote
from uuid import uuid4


def checked_credentials(email: str, name: str, password: str, secret: str, code: str,
                        now: datetime) -> tuple[str, str, int]:
    from metahub_control.identity.service import normalize_global_email, validate_global_password
    from metahub_shared.identity import totp_counter

    email = normalize_global_email(email)
    name = name.strip()
    if email is None or not 1 <= len(name) <= 120 or any(ord(c) < 32 for c in name):
        raise ValueError("Invalid owner identity.")
    validate_global_password(password)
    if len(password) < 14:
        raise ValueError("First-owner password must contain at least 14 characters.")
    counter = totp_counter(code, secret, unix_time=int(now.timestamp()))
    if counter is None:
        raise ValueError("Authenticator code did not verify; nothing was changed.")
    return email, name, counter


def provision(connection, *, email: str, name: str, password: str, secret: str,
              code: str, encryption_root: str, now: datetime) -> dict[str, str]:
    from metahub_shared.db.security import hash_password_argon2id
    from metahub_shared.identity import MfaSecretOwner, encrypt_mfa_secret

    email, name, counter = checked_credentials(email, name, password, secret, code, now)
    account, factor, operator = (prefix + uuid4().hex for prefix in ("gac_", "mfa_", "pto_"))
    timestamp = now.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    hashed = hash_password_argon2id(password)
    encrypted = encrypt_mfa_secret(secret, secret=encryption_root,
                                   owner=MfaSecretOwner(account, factor))
    with connection.transaction():
        # Serialize this tool and also block normal account/tenant insertions
        # during the empty-database check. No existing account can be promoted.
        connection.execute("SELECT pg_advisory_xact_lock(1296389185)")
        connection.execute("LOCK TABLE global_accounts, platform_operators, platform_bootstrap_state, tenants, users IN EXCLUSIVE MODE")
        for table in ("global_accounts", "platform_operators", "tenants", "users"):
            if connection.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone():
                raise ValueError("Database already contains identities or business data; initialization refused.")
        row = connection.execute("SELECT status FROM platform_bootstrap_state WHERE singleton_id=1 FOR UPDATE").fetchone()
        if row is None or row[0] != "open":
            raise ValueError("First-owner initialization is already closed or schema is incomplete.")
        connection.execute("""INSERT INTO global_accounts(
          account_id,email_normalized,display_name,status,login_enabled,credential_revision,
          session_revision,mfa_state,failed_login_count,created_at,updated_at)
          VALUES(%s,%s,%s,'active',1,1,1,'not_enrolled',0,%s,%s)""",
          (account, email, name, timestamp, timestamp))
        connection.execute("""INSERT INTO account_credentials(
          credential_id,account_id,credential_type,algorithm,secret_hash,status,version,created_at,updated_at)
          VALUES(%s,%s,'password','argon2id',%s,'active',1,%s,%s)""",
          ("acr_" + uuid4().hex, account, hashed, timestamp, timestamp))
        connection.execute("""INSERT INTO account_mfa_factors(
          factor_id,account_id,factor_type,secret_ciphertext,secret_version,status,revision,
          last_accepted_counter,verified_at,created_at,updated_at)
          VALUES(%s,%s,'totp',%s,1,'active',1,%s,%s,%s,%s)""",
          (factor, account, encrypted, counter, timestamp, timestamp, timestamp))
        connection.execute("""INSERT INTO platform_operators(
          operator_id,account_id,status,revision,created_by_operator_id,created_at,updated_at)
          VALUES(%s,%s,'active',1,NULL,%s,%s)""", (operator, account, timestamp, timestamp))
        connection.execute("""INSERT INTO platform_operator_role_bindings(
          operator_id,role_code,status,granted_by_operator_id,granted_at,revoked_at)
          VALUES(%s,'platform_owner','active',NULL,%s,NULL)""", (operator, timestamp))
        connection.execute("""UPDATE platform_bootstrap_state
          SET status='completed',operator_id=%s,completed_at=%s WHERE singleton_id=1""", (operator, timestamp))
        # Root possession attests installation, not an email-delivery challenge.
        # Do not falsely record an email verification that never took place.
        connection.execute("""INSERT INTO platform_audit_logs(
          audit_id,actor_domain,actor_id,action_code,object_type,object_id,reason_code,
          request_id,trace_id,occurred_at)
          VALUES(%s,'platform',%s,'platform.bootstrap_completed','platform_operator',%s,
          'offline_first_install',%s,%s,%s)""",
          ("pau_" + uuid4().hex, operator, operator, "install_" + uuid4().hex,
           "install_" + uuid4().hex, timestamp))
    return {"status": "initialized", "account_id": account, "operator_id": operator}


def main() -> int:
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print("Use an interactive terminal; credential input and enrollment output cannot be redirected.", file=sys.stderr)
        return 1
    try:
        # In the image this shares the exact service preflight and secret loader.
        sys.path.insert(0, "/app")
        from service import load_secrets, validate_production
        from metahub_shared.runtime.config import load_app_config
        from metahub_shared.identity import generate_totp_secret
        import psycopg

        load_secrets(Path("/run/secrets/runtime.env"))
        validate_production(dict(os.environ))
        config = load_app_config("control-plane-api")
        email = input("First-owner email: ").strip()
        name = input("Display name: ").strip()
        password = getpass.getpass("New password (at least 14 characters): ")
        if password != getpass.getpass("Confirm new password: "):
            raise ValueError("Passwords do not match.")
        secret = generate_totp_secret()
        uri = f"otpauth://totp/{quote('MetaHub:' + email, safe='')}?secret={secret}&issuer=MetaHub&algorithm=SHA1&digits=6&period=30"
        print("Add this secret to your authenticator. Keep it private:")
        print(secret)
        print(uri)
        code = getpass.getpass("Current authenticator code: ")
        with psycopg.connect(config.database_url, autocommit=True,
                             options="-c timezone=UTC -c statement_timeout=30000 -c lock_timeout=10000") as connection:
            result = provision(connection, email=email, name=name, password=password,
                               secret=secret, code=code, encryption_root=config.identity_mfa_encryption_secret,
                               now=datetime.now(UTC))
        print(json.dumps(result))
        print("Wait for a fresh authenticator code before logging in. Initialization cannot be repeated.")
        return 0
    except Exception as error:
        # Driver errors may include inserted values. Never emit their message.
        print(json.dumps({"status": "refused", "exception_type": type(error).__name__}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
