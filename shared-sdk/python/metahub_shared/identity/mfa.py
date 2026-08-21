"""Owner-bound TOTP secret encryption and replay-safe code verification."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import struct
from dataclasses import dataclass
from secrets import token_bytes

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

MFA_CIPHERTEXT_FORMAT = "metahub-identity-mfa-aesgcm-v1"
DEFAULT_IDENTITY_MFA_ENCRYPTION_SECRET = (
    "dev-only-change-me-metahub-identity-mfa-encryption-secret-2026"
)
_KEY_DOMAIN = b"metahub/prd095/identity-mfa/aesgcm/v1"


class MfaCryptoError(ValueError):
    """Fixed failure used to avoid secret and factor-state oracles."""


@dataclass(frozen=True, slots=True)
class MfaSecretOwner:
    account_id: str
    factor_id: str
    key_version: int = 1


def generate_totp_secret() -> str:
    """Return a canonical 160-bit Base32 secret without padding."""

    return base64.b32encode(token_bytes(20)).decode("ascii").rstrip("=")


def encrypt_mfa_secret(value: str, *, secret: str, owner: MfaSecretOwner) -> str:
    checked = _owner(owner)
    plaintext = _decode_totp_secret(value)
    nonce = token_bytes(12)
    try:
        ciphertext = AESGCM(_key(secret)).encrypt(nonce, plaintext, _aad(checked))
    except MfaCryptoError:
        raise
    except Exception:
        raise MfaCryptoError("identity.mfa_unavailable") from None
    return ":".join(
        (
            MFA_CIPHERTEXT_FORMAT,
            str(checked.key_version),
            _b64(nonce),
            _b64(ciphertext),
        )
    )


def decrypt_mfa_secret(value: str, *, secret: str, owner: MfaSecretOwner) -> str:
    checked = _owner(owner)
    if type(value) is not str:
        raise MfaCryptoError("identity.mfa_unavailable")
    parts = value.split(":")
    if len(parts) != 4 or parts[:2] != [MFA_CIPHERTEXT_FORMAT, str(checked.key_version)]:
        raise MfaCryptoError("identity.mfa_unavailable")
    try:
        nonce = _unb64(parts[2])
        ciphertext = _unb64(parts[3])
        if len(nonce) != 12 or len(ciphertext) < 17:
            raise ValueError
        plaintext = AESGCM(_key(secret)).decrypt(nonce, ciphertext, _aad(checked))
        if len(plaintext) != 20:
            raise ValueError
        return base64.b32encode(plaintext).decode("ascii").rstrip("=")
    except MfaCryptoError:
        raise
    except (InvalidTag, binascii.Error, UnicodeError, ValueError, TypeError):
        raise MfaCryptoError("identity.mfa_unavailable") from None
    except Exception:
        raise MfaCryptoError("identity.mfa_unavailable") from None


def totp_counter(code: str, base32_secret: str, *, unix_time: int, skew: int = 1) -> int | None:
    """Return the matching counter in a bounded window, without disclosing skew direction."""

    if type(code) is not str or len(code) != 6 or not code.isascii() or not code.isdigit():
        return None
    if type(unix_time) is not int or type(skew) is not int or skew < 0 or skew > 1:
        return None
    key = _decode_totp_secret(base32_secret)
    center = unix_time // 30
    for candidate in range(center - skew, center + skew + 1):
        digest = hmac.new(key, struct.pack(">Q", candidate), hashlib.sha1).digest()
        offset = digest[-1] & 0x0F
        binary = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
        expected = f"{binary % 1_000_000:06d}"
        if hmac.compare_digest(code, expected):
            return candidate
    return None


def _owner(owner: object) -> MfaSecretOwner:
    if (
        not isinstance(owner, MfaSecretOwner)
        or not owner.account_id.startswith("gac_")
        or not owner.factor_id.startswith("mfa_")
        or owner.key_version != 1
    ):
        raise MfaCryptoError("identity.mfa_unavailable")
    return owner


def _aad(owner: MfaSecretOwner) -> bytes:
    return json.dumps(
        [MFA_CIPHERTEXT_FORMAT, "totp", owner.account_id, owner.factor_id, owner.key_version],
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("ascii")


def _key(secret: object) -> bytes:
    if type(secret) is not str or secret != secret.strip():
        raise MfaCryptoError("identity.mfa_unavailable")
    encoded = secret.encode("utf-8", errors="strict")
    if not 32 <= len(encoded) <= 512 or any(ord(character) < 33 for character in secret):
        raise MfaCryptoError("identity.mfa_unavailable")
    return hmac.new(encoded, _KEY_DOMAIN, hashlib.sha256).digest()


def _decode_totp_secret(value: object) -> bytes:
    if type(value) is not str or len(value) != 32 or not value.isascii():
        raise MfaCryptoError("identity.mfa_unavailable")
    try:
        decoded = base64.b32decode(value + "=" * ((8 - len(value) % 8) % 8), casefold=False)
    except (binascii.Error, ValueError):
        raise MfaCryptoError("identity.mfa_unavailable") from None
    if len(decoded) != 20 or base64.b32encode(decoded).decode("ascii").rstrip("=") != value:
        raise MfaCryptoError("identity.mfa_unavailable")
    return decoded


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii")


def _unb64(value: str) -> bytes:
    if type(value) is not str or not value:
        raise binascii.Error
    decoded = base64.b64decode(value.encode("ascii"), altchars=b"-_", validate=True)
    if _b64(decoded) != value:
        raise binascii.Error
    return decoded
