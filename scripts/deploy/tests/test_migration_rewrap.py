import importlib.util
from pathlib import Path
import secrets
import sqlite3
import sys

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "shared-sdk/python"))
spec = importlib.util.spec_from_file_location("rewrap", ROOT / "scripts/deploy/rewrap-migration-secrets.py")
tool = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tool)


def test_key_and_totp_values_survive_encryption_root_rotation():
    from metahub_shared.identity.mfa import generate_totp_secret
    old_key, new_key, new_mfa = (secrets.token_hex(32) for _ in range(3))
    totp, key = generate_totp_secret(), secrets.token_urlsafe(32)
    owner = tool.MfaSecretOwner("gac_" + "1" * 32, "mfa_" + "2" * 32)
    connection = sqlite3.connect(":memory:")
    for table in ("virtual_keys", "provider_credentials"):
        connection.execute(f"CREATE TABLE {table} (encrypted_key TEXT,key_hash TEXT)")
        connection.execute(f"INSERT INTO {table} VALUES(?,?)", (tool.encrypt_secret(key, encryption_secret=old_key), "unchanged-key-hash"))
    connection.execute("CREATE TABLE account_mfa_factors(account_id TEXT,factor_id TEXT,secret_version INTEGER,secret_ciphertext TEXT,factor_type TEXT)")
    connection.execute("INSERT INTO account_mfa_factors VALUES(?,?,1,?,'totp')", (owner.account_id, owner.factor_id, tool.encrypt_mfa_secret(totp, secret=tool.DEFAULT_IDENTITY_MFA_ENCRYPTION_SECRET, owner=owner)))
    old = {"METAHUB_PROVIDER_KEY_ENCRYPTION_SECRET": old_key}
    new = {"METAHUB_PROVIDER_KEY_ENCRYPTION_SECRET": new_key, "METAHUB_IDENTITY_MFA_ENCRYPTION_SECRET": new_mfa}
    result = tool.rewrap(connection, old, new, allow_development=True)
    assert result == {"provider_credentials": 1, "virtual_keys": 1, "account_mfa_factors": 1, "invalid_mfa_preserved": 0}
    for table in ("virtual_keys", "provider_credentials"):
        encrypted, key_hash = connection.execute(f"SELECT * FROM {table}").fetchone()
        assert tool.decrypt_secret(encrypted, encryption_secret=new_key) == key
        assert key_hash == "unchanged-key-hash"
    encrypted = connection.execute("SELECT secret_ciphertext FROM account_mfa_factors").fetchone()[0]
    assert tool.decrypt_mfa_secret(encrypted, secret=new_mfa, owner=owner) == totp


def test_plaintext_or_unknown_ciphertext_is_not_silently_accepted():
    connection = sqlite3.connect(":memory:")
    for table in ("virtual_keys", "provider_credentials"):
        connection.execute(f"CREATE TABLE {table} (encrypted_key TEXT)")
    connection.execute("INSERT INTO virtual_keys VALUES('plaintext-secret')")
    roots = {"METAHUB_PROVIDER_KEY_ENCRYPTION_SECRET": secrets.token_hex(32), "METAHUB_IDENTITY_MFA_ENCRYPTION_SECRET": secrets.token_hex(32)}
    with pytest.raises(ValueError):
        tool.rewrap(connection, roots, roots, allow_development=False)


def test_invalid_legacy_mfa_requires_explicit_preservation_and_remains_unusable():
    connection = sqlite3.connect(":memory:")
    for table in ("virtual_keys", "provider_credentials"):
        connection.execute(f"CREATE TABLE {table} (encrypted_key TEXT)")
    connection.execute("CREATE TABLE account_mfa_factors(account_id TEXT,factor_id TEXT,secret_version INTEGER,secret_ciphertext TEXT,factor_type TEXT)")
    connection.execute("INSERT INTO account_mfa_factors VALUES(?,?,1,?,'totp')", ("gac_" + "1" * 32, "mfa_" + "2" * 32, "malformed-legacy-factor"))
    roots = {"METAHUB_PROVIDER_KEY_ENCRYPTION_SECRET": secrets.token_hex(32), "METAHUB_IDENTITY_MFA_ENCRYPTION_SECRET": secrets.token_hex(32)}
    with pytest.raises(ValueError):
        tool.rewrap(connection, roots, roots, allow_development=False)
    result = tool.rewrap(connection, roots, roots, allow_development=False, preserve_invalid_mfa=True)
    assert result["invalid_mfa_preserved"] == 1
    assert result["account_mfa_factors"] == 0
    assert connection.execute("SELECT secret_ciphertext FROM account_mfa_factors").fetchone()[0] == "malformed-legacy-factor"
