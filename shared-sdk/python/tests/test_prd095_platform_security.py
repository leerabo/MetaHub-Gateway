"""PRD-095 secret-domain and TOTP primitive contracts."""

from __future__ import annotations

import base64
import hashlib
import hmac
import struct

import pytest
from metahub_shared.identity import (
    MfaCryptoError,
    MfaSecretOwner,
    decrypt_mfa_secret,
    encrypt_mfa_secret,
    generate_totp_secret,
    totp_counter,
)
from metahub_shared.runtime.config import ConfigError, load_app_config


def _code(secret: str, counter: int) -> str:
    key = base64.b32decode(secret + "=" * ((8 - len(secret) % 8) % 8))
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 15
    binary = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return f"{binary % 1_000_000:06d}"


def test_mfa_secret_is_owner_bound_and_totp_window_is_bounded() -> None:
    owner = MfaSecretOwner(
        "gac_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "mfa_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    )
    secret = generate_totp_secret()
    root = "prd095-mfa-root-secret-0123456789abcdef"
    encrypted = encrypt_mfa_secret(secret, secret=root, owner=owner)
    assert secret not in encrypted
    assert decrypt_mfa_secret(encrypted, secret=root, owner=owner) == secret
    with pytest.raises(MfaCryptoError):
        decrypt_mfa_secret(
            encrypted,
            secret=root,
            owner=MfaSecretOwner(
                owner.account_id,
                "mfa_cccccccccccccccccccccccccccccccc",
            ),
        )
    unix_time = 1_800_000_000
    counter = unix_time // 30
    assert totp_counter(_code(secret, counter), secret, unix_time=unix_time) == counter
    assert totp_counter(_code(secret, counter + 2), secret, unix_time=unix_time) is None


def test_platform_secret_domains_are_masked_and_reuse_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values = {
        "METAHUB_ENV": "test",
        "METAHUB_PLATFORM_SESSION_SECRET": "platform-session-secret-0123456789abcdef",
        "METAHUB_SUPPORT_SESSION_SECRET": "support-session-secret-0123456789abcdef0",
        "METAHUB_IDENTITY_MFA_ENCRYPTION_SECRET": "identity-mfa-secret-0123456789abcdef0",
        "METAHUB_PLATFORM_BOOTSTRAP_RECOVERY_SECRET": "bootstrap-recovery-secret-0123456789abcdef",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    config = load_app_config("control-plane-api")
    dumped = config.model_dump()
    assert not {
        "platform_session_secret",
        "support_session_secret",
        "identity_mfa_encryption_secret",
        "platform_bootstrap_recovery_secret",
    } & dumped.keys()
    monkeypatch.setenv(
        "METAHUB_SUPPORT_SESSION_SECRET", values["METAHUB_PLATFORM_SESSION_SECRET"]
    )
    with pytest.raises(ConfigError):
        load_app_config("control-plane-api")
