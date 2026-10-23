"""Re-encrypt credentials on a NEW SQLite copy; retain passwords, KEYs and TOTP.

Development roots may be used only as decryption inputs to this offline tool.
They are never written into the production runtime configuration.
"""
from __future__ import annotations

import argparse
import hmac
import json
from pathlib import Path
import sqlite3
import sys

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "shared-sdk/python"))
from metahub_shared.db.security import decrypt_secret, encrypt_secret
from metahub_shared.identity.mfa import DEFAULT_IDENTITY_MFA_ENCRYPTION_SECRET, MfaSecretOwner, decrypt_mfa_secret, encrypt_mfa_secret


def rewrap(connection, old: dict, new: dict, *, allow_development: bool, preserve_invalid_mfa: bool = False) -> dict:
    counts = {"provider_credentials": 0, "virtual_keys": 0, "account_mfa_factors": 0, "invalid_mfa_preserved": 0}
    provider_old = old.get("METAHUB_PROVIDER_KEY_ENCRYPTION_SECRET") or old.get("METAHUB_AUTH_TOKEN_SECRET")
    provider_new = new.get("METAHUB_PROVIDER_KEY_ENCRYPTION_SECRET", "")
    mfa_old = old.get("METAHUB_IDENTITY_MFA_ENCRYPTION_SECRET")
    mfa_new = new.get("METAHUB_IDENTITY_MFA_ENCRYPTION_SECRET", "")
    for secret in (provider_new, mfa_new):
        if len(secret) < 32 or secret.startswith(("dev-only", "replace-")):
            raise ValueError("Dedicated production encryption roots are required.")
    for table in ("provider_credentials", "virtual_keys"):
        rows = connection.execute(f'SELECT rowid,encrypted_key FROM "{table}" WHERE encrypted_key IS NOT NULL').fetchall()
        for rowid, encrypted in rows:
            if encrypted.startswith("local-placeholder:"):
                continue  # An unusable placeholder contains no recoverable credential.
            if not provider_old:
                raise ValueError("Original Provider/KEY encryption root is missing.")
            plaintext = decrypt_secret(encrypted, encryption_secret=provider_old)
            replacement = encrypt_secret(plaintext, encryption_secret=provider_new)
            if not hmac.compare_digest(plaintext, decrypt_secret(replacement, encryption_secret=provider_new)):
                raise ValueError("Credential rewrap failed verification.")
            connection.execute(f'UPDATE "{table}" SET encrypted_key=? WHERE rowid=?', (replacement, rowid))
            counts[table] += 1
    factors = connection.execute("SELECT rowid,account_id,factor_id,secret_version,secret_ciphertext FROM account_mfa_factors WHERE factor_type='totp' AND secret_ciphertext IS NOT NULL").fetchall()
    if factors and not mfa_old:
        if not allow_development:
            raise ValueError("Original MFA root is missing; explicitly authorize offline development-root decryption.")
        mfa_old = DEFAULT_IDENTITY_MFA_ENCRYPTION_SECRET
    for rowid, account_id, factor_id, version, encrypted in factors:
        # Legacy test setups sometimes inserted a dummy value instead of an
        # encrypted factor. Preserve it only when explicitly requested; never
        # replace it with a bypass or silently claim successful decryption.
        if not encrypted.startswith("metahub-identity-mfa-aesgcm-v1:") and preserve_invalid_mfa:
            counts["invalid_mfa_preserved"] += 1
            continue
        owner = MfaSecretOwner(account_id=account_id, factor_id=factor_id, key_version=version)
        plaintext = decrypt_mfa_secret(encrypted, secret=mfa_old, owner=owner)
        replacement = encrypt_mfa_secret(plaintext, secret=mfa_new, owner=owner)
        if not hmac.compare_digest(plaintext, decrypt_mfa_secret(replacement, secret=mfa_new, owner=owner)):
            raise ValueError("MFA rewrap failed verification.")
        connection.execute("UPDATE account_mfa_factors SET secret_ciphertext=? WHERE rowid=?", (replacement, rowid))
        counts["account_mfa_factors"] += 1
    # These domains use separate owner-bound envelopes. Fail closed rather than
    # silently losing access to files, prompts, SSO or other encrypted records.
    for (table,) in connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
        for column in connection.execute('PRAGMA table_info("' + table.replace('"', '""') + '")').fetchall():
            name = column[1]
            if not any(token in name for token in ("encrypted", "ciphertext")):
                continue
            if (table in ("provider_credentials", "virtual_keys") and name == "encrypted_key") or (table == "account_mfa_factors" and name == "secret_ciphertext"):
                continue
            quoted_table, quoted_column = table.replace('"', '""'), name.replace('"', '""')
            if connection.execute(f'SELECT 1 FROM "{quoted_table}" WHERE "{quoted_column}" IS NOT NULL AND length("{quoted_column}")>0 LIMIT 1').fetchone():
                raise ValueError("Additional encryption domain requires a dedicated migration adapter; copy was rolled back.")
    return counts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--old-secrets", type=Path, required=True)
    parser.add_argument("--new-secrets", type=Path, required=True)
    parser.add_argument("--allow-development-root", action="store_true")
    parser.add_argument("--preserve-invalid-mfa", action="store_true", help="Retain malformed legacy MFA rows byte-for-byte and report them; affected accounts still require factor recovery.")
    args = parser.parse_args()
    if not args.source.is_file() or args.output.exists():
        raise ValueError("Source must exist, and output must be a NEW copy.")
    original = sqlite3.connect(args.source.resolve().as_uri() + "?mode=ro", uri=True)
    copy = sqlite3.connect(args.output)
    try:
        original.backup(copy)
        # Password hashes and account records are outside the update projection.
        accounts_before = copy.execute("SELECT * FROM global_accounts ORDER BY account_id").fetchall()
        copy.execute("BEGIN IMMEDIATE")
        counts = rewrap(copy, dict(dotenv_values(args.old_secrets)), dict(dotenv_values(args.new_secrets)), allow_development=args.allow_development_root, preserve_invalid_mfa=args.preserve_invalid_mfa)
        if copy.execute("SELECT * FROM global_accounts ORDER BY account_id").fetchall() != accounts_before:
            raise ValueError("Account preservation check failed.")
        copy.commit()
        print(json.dumps({"status": "verified", "rewrapped": counts, "account_records_unchanged": True, "plaintext_credentials_unchanged": True, "source_modified": False}))
        return 0
    finally:
        original.close()
        copy.close()
        args.output.chmod(0o600)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(json.dumps({"status": "error", "exception_type": type(error).__name__, "phase": "credential_rewrap"}), file=sys.stderr)
        raise SystemExit(1) from None
