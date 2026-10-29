"""Owner-bound AES-256-GCM envelopes for short-lived Global Identity PII."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import re
from dataclasses import dataclass
from secrets import token_bytes
from typing import Literal

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

IdentityPiiPurpose = Literal["registration"]
IdentityPiiFieldKind = Literal["email_normalized"]

IDENTITY_PII_CIPHERTEXT_FORMAT = "metahub-identity-pii-aesgcm-v1"
DEFAULT_IDENTITY_PII_ENCRYPTION_SECRET = (
    "dev-only-change-me-metahub-identity-pii-encryption-secret-2026"
)

_KEY_DOMAIN = b"metahub/prd092/identity-pii/aesgcm/v1"
_INTENT_ID = re.compile(r"^rgi_[0-9a-f]{32}$")
_MAX_PLAINTEXT_BYTES = 320


class IdentityPiiCryptoError(ValueError):
    """Fixed cryptographic failure; callers must expose only identity.unavailable."""


@dataclass(frozen=True, slots=True)
class IdentityPiiOwner:
    """Bind a ciphertext to one intent, purpose, field, and key version."""

    purpose: IdentityPiiPurpose
    intent_id: str
    field_kind: IdentityPiiFieldKind
    key_version: int = 1


def encrypt_identity_pii(value: object, *, secret: object, owner: IdentityPiiOwner) -> str:
    """Encrypt bounded UTF-8 PII and return only an authenticated ASCII envelope."""

    checked = _owner(owner)
    plaintext = _plaintext(value)
    nonce = token_bytes(12)
    try:
        ciphertext = AESGCM(_key(secret)).encrypt(nonce, plaintext, _aad(checked))
    except IdentityPiiCryptoError:
        raise
    except Exception:
        raise IdentityPiiCryptoError("identity.unavailable") from None
    return ":".join(
        (
            IDENTITY_PII_CIPHERTEXT_FORMAT,
            str(checked.key_version),
            checked.field_kind,
            _b64(nonce),
            _b64(ciphertext),
        )
    )


def decrypt_identity_pii(value: object, *, secret: object, owner: IdentityPiiOwner) -> str:
    """Authenticate one exact owner and return plaintext only to the current call stack."""

    checked = _owner(owner)
    if type(value) is not str:
        raise IdentityPiiCryptoError("identity.unavailable")
    parts = value.split(":")
    expected = [
        IDENTITY_PII_CIPHERTEXT_FORMAT,
        str(checked.key_version),
        checked.field_kind,
    ]
    if len(parts) != 5 or parts[:3] != expected:
        raise IdentityPiiCryptoError("identity.unavailable")
    try:
        nonce = _unb64(parts[3])
        ciphertext = _unb64(parts[4])
        if len(nonce) != 12 or len(ciphertext) < 17:
            raise ValueError
        plaintext = AESGCM(_key(secret)).decrypt(nonce, ciphertext, _aad(checked))
        return _plaintext(plaintext).decode("utf-8", errors="strict")
    except IdentityPiiCryptoError:
        raise
    except (InvalidTag, binascii.Error, UnicodeError, ValueError, TypeError):
        raise IdentityPiiCryptoError("identity.unavailable") from None
    except Exception:
        raise IdentityPiiCryptoError("identity.unavailable") from None


def _owner(owner: object) -> IdentityPiiOwner:
    if (
        not isinstance(owner, IdentityPiiOwner)
        or owner.purpose != "registration"
        or owner.field_kind != "email_normalized"
        or type(owner.intent_id) is not str
        or _INTENT_ID.fullmatch(owner.intent_id) is None
        or type(owner.key_version) is not int
        or owner.key_version != 1
    ):
        raise IdentityPiiCryptoError("identity.unavailable")
    return owner


def _aad(owner: IdentityPiiOwner) -> bytes:
    return json.dumps(
        [
            IDENTITY_PII_CIPHERTEXT_FORMAT,
            owner.purpose,
            owner.intent_id,
            owner.field_kind,
            owner.key_version,
        ],
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("ascii")


def _key(secret: object) -> bytes:
    if type(secret) is not str or secret != secret.strip():
        raise IdentityPiiCryptoError("identity.unavailable")
    try:
        encoded = secret.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        raise IdentityPiiCryptoError("identity.unavailable") from None
    if not 32 <= len(encoded) <= 512 or any(ord(character) < 33 for character in secret):
        raise IdentityPiiCryptoError("identity.unavailable")
    return hmac.new(encoded, _KEY_DOMAIN, hashlib.sha256).digest()


def _plaintext(value: object) -> bytes:
    if type(value) is str:
        try:
            encoded = value.encode("utf-8", errors="strict")
        except UnicodeEncodeError:
            raise IdentityPiiCryptoError("identity.unavailable") from None
    elif type(value) is bytes:
        encoded = value
        try:
            encoded.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            raise IdentityPiiCryptoError("identity.unavailable") from None
    else:
        raise IdentityPiiCryptoError("identity.unavailable")
    if not encoded or len(encoded) > _MAX_PLAINTEXT_BYTES:
        raise IdentityPiiCryptoError("identity.unavailable")
    return encoded


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii")


def _unb64(value: str) -> bytes:
    if type(value) is not str or not value:
        raise binascii.Error
    decoded = base64.b64decode(value.encode("ascii"), altchars=b"-_", validate=True)
    if _b64(decoded) != value:
        raise binascii.Error
    return decoded
