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
from uuid import uuid4


class FirstOwnerError(ValueError):
    """Expected refusal with a static message safe to show on the terminal."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def checked_password(password: str) -> None:
    from metahub_control.identity.service import validate_global_password
    from metahub_shared.runtime.errors import ApiError

    if len(password) < 8:
        raise FirstOwnerError("password_too_short", "Password must contain at least 8 characters. / 密码至少需要 8 个字符。")
    try:
        validate_global_password(password)
    except ApiError:
        raise FirstOwnerError("password_invalid", "Password must be valid UTF-8, at most 1024 bytes, without NUL characters. / 密码不能超过 1024 字节，且不能包含空字符。") from None


def read_password() -> str:
    password = getpass.getpass("New password (at least 8 characters): ")
    checked_password(password)
    if password != getpass.getpass("Confirm new password: "):
        raise FirstOwnerError("password_mismatch", "Passwords do not match. / 两次输入的密码不一致，请重新执行命令。")
    return password


def checked_credentials(email: str, name: str, password: str, secret: str | None, code: str | None,
                        now: datetime) -> tuple[str, str, int | None]:
    from metahub_control.identity.service import normalize_global_email
    from metahub_shared.identity import totp_counter

    email = normalize_global_email(email)
    name = name.strip()
    if email is None or not 1 <= len(name) <= 120 or any(ord(c) < 32 for c in name):
        raise FirstOwnerError("identity_invalid", "Enter a valid email and a display name of 1–120 characters without control characters. / 请填写有效邮箱和 1–120 个字符的显示名称。")
    checked_password(password)
    if secret is None and code is None:
        return email, name, None
    counter = totp_counter(code or "", secret or "", unix_time=int(now.timestamp()))
    if counter is None:
        raise FirstOwnerError("authenticator_invalid", "Authenticator code did not verify; nothing was changed. / 动态验证码错误，未进行任何修改。")
    return email, name, counter


def provision(connection, *, email: str, name: str, password: str,
              encryption_root: str, now: datetime, secret: str | None = None,
              code: str | None = None) -> dict[str, str]:
    from metahub_shared.db.security import hash_password_argon2id
    from metahub_shared.identity import MfaSecretOwner, encrypt_mfa_secret

    email, name, counter = checked_credentials(email, name, password, secret, code, now)
    account, factor, operator = (prefix + uuid4().hex for prefix in ("gac_", "mfa_", "pto_"))
    timestamp = now.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    hashed = hash_password_argon2id(password)
    encrypted = (encrypt_mfa_secret(secret, secret=encryption_root,
                                   owner=MfaSecretOwner(account, factor)) if secret else None)
    with connection.transaction():
        # Serialize this tool and also block normal account/tenant insertions
        # during the empty-database check. No existing account can be promoted.
        connection.execute("SELECT pg_advisory_xact_lock(1296389185)")
        connection.execute("LOCK TABLE global_accounts, platform_operators, platform_bootstrap_state, tenants, users IN EXCLUSIVE MODE")
        for table in ("global_accounts", "platform_operators", "tenants", "users"):
            if connection.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone():
                raise FirstOwnerError("database_not_empty", "Database already contains identities or business data; initialization refused. / 数据库已有账号或业务数据，不能重复初始化。")
        row = connection.execute("SELECT status FROM platform_bootstrap_state WHERE singleton_id=1 FOR UPDATE").fetchone()
        if row is None or row[0] != "open":
            raise FirstOwnerError("bootstrap_closed", "First-owner initialization is already closed or schema is incomplete. / 初始化已关闭，或数据库迁移尚未完成。")
        connection.execute("""INSERT INTO global_accounts(
          account_id,email_normalized,display_name,status,login_enabled,credential_revision,
          session_revision,mfa_state,failed_login_count,created_at,updated_at)
          VALUES(%s,%s,%s,'active',1,1,1,'not_enrolled',0,%s,%s)""",
          (account, email, name, timestamp, timestamp))
        connection.execute("""INSERT INTO account_credentials(
          credential_id,account_id,credential_type,algorithm,secret_hash,status,version,created_at,updated_at)
          VALUES(%s,%s,'password','argon2id',%s,'active',1,%s,%s)""",
          ("acr_" + uuid4().hex, account, hashed, timestamp, timestamp))
        if encrypted is not None:
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
        import psycopg

        load_secrets(Path("/run/secrets/runtime.env"))
        validate_production(dict(os.environ))
        config = load_app_config("control-plane-api")
        email = input("First-owner email: ").strip()
        name = input("Display name: ").strip()
        password = read_password()
        checked_credentials(email, name, password, None, None, datetime.now(UTC))
        with psycopg.connect(config.database_url, autocommit=True,
                             options="-c timezone=UTC -c statement_timeout=30000 -c lock_timeout=10000") as connection:
            result = provision(connection, email=email, name=name, password=password,
                               encryption_root=config.identity_mfa_encryption_secret,
                               now=datetime.now(UTC))
        print(json.dumps(result))
        print("Sign in with your email and password. MFA is optional and can be enabled in account security. Initialization cannot be repeated.")
        return 0
    except FirstOwnerError as error:
        print(json.dumps({"status": "refused", "reason": error.code,
                          "message": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 1
    except Exception as error:
        # Driver errors may include inserted values. Never emit their message.
        print(json.dumps({"status": "refused", "exception_type": type(error).__name__}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
