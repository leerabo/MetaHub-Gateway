"""Owner-bound encryption for short-lived PRD-134 identity exports."""

from __future__ import annotations

import base64
import hashlib
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError


def encrypt_identity_export(
    config: AppConfig,
    plaintext: bytes,
    *,
    export_id: str,
    workspace_id: str,
    tenant_id: str,
    account_id: str,
) -> tuple[str, str, str, str]:
    plaintext_sha256 = hashlib.sha256(plaintext).hexdigest()
    nonce = os.urandom(12)
    ciphertext = AESGCM(_key(config)).encrypt(
        nonce,
        plaintext,
        _aad(export_id, workspace_id, tenant_id, account_id, plaintext_sha256),
    )
    return _b64(ciphertext), _b64(nonce), hashlib.sha256(ciphertext).hexdigest(), plaintext_sha256


def decrypt_identity_export(
    config: AppConfig,
    ciphertext_b64: str,
    nonce_b64: str,
    *,
    export_id: str,
    workspace_id: str,
    tenant_id: str,
    account_id: str,
    plaintext_sha256: str,
    artifact_sha256: str,
) -> bytes:
    try:
        ciphertext = _unb64(ciphertext_b64)
        if hashlib.sha256(ciphertext).hexdigest() != artifact_sha256:
            raise ValueError
        plaintext = AESGCM(_key(config)).decrypt(
            _unb64(nonce_b64),
            ciphertext,
            _aad(export_id, workspace_id, tenant_id, account_id, plaintext_sha256),
        )
        if hashlib.sha256(plaintext).hexdigest() != plaintext_sha256:
            raise ValueError
        return plaintext
    except ApiError:
        raise
    except (InvalidTag, ValueError, TypeError):
        raise ApiError("privacy.export_corrupt") from None


def _key(config: AppConfig) -> bytes:
    secret = config.tenant_data_encryption_secret
    if not secret or len(secret.encode("utf-8")) < 32:
        raise ApiError("privacy.kms_unavailable")
    return hashlib.sha256(
        b"prd134/identity-export/a256gcm/v1\0"
        + str(config.tenant_data_encryption_version).encode("ascii")
        + b"\0"
        + secret.encode("utf-8")
    ).digest()


def _aad(
    export_id: str,
    workspace_id: str,
    tenant_id: str,
    account_id: str,
    digest: str,
) -> bytes:
    return (
        f"prd134/identity-export/v1\0{export_id}\0{workspace_id}\0{tenant_id}\0"
        f"{account_id}\0{digest}"
    ).encode("ascii")


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _unb64(value: str) -> bytes:
    if not value or "=" in value:
        raise ValueError
    return base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)


__all__ = ["decrypt_identity_export", "encrypt_identity_export"]
