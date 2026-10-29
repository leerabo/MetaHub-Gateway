"""Owner-bound cryptographic primitives for enterprise SSO control data."""

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

EnterpriseSsoSecretPurpose = Literal["oidc_client_secret", "pkce_verifier"]

ENTERPRISE_SSO_CIPHERTEXT_FORMAT = "metahub-enterprise-sso-aesgcm-v1"
_ENCRYPTION_DOMAIN = b"metahub/prd114/enterprise-sso/aesgcm/v1"
_HMAC_DOMAIN = b"metahub/prd114/enterprise-sso/hmac/v1"
_OWNER_ID = re.compile(r"^(?:ssc|sls)_[0-9a-f]{32}$")
_WORKSPACE_ID = re.compile(r"^wsp_[0-9a-f]{32}$")
_PURPOSE = re.compile(r"^[a-z][a-z0-9_]{2,63}$")


class EnterpriseSsoCryptoError(ValueError):
    """Fixed cryptographic failure that must map to a non-sensitive API error."""


@dataclass(frozen=True, slots=True)
class EnterpriseSsoSecretOwner:
    workspace_id: str
    tenant_id: str
    resource_id: str
    purpose: EnterpriseSsoSecretPurpose
    key_version: int = 1


def enterprise_sso_hmac(secret: object, purpose: object, value: object) -> str:
    """Return a stable purpose-separated digest for a bounded UTF-8 value."""

    key = _secret_key(secret, _HMAC_DOMAIN)
    if type(purpose) is not str or _PURPOSE.fullmatch(purpose) is None:
        raise EnterpriseSsoCryptoError("sso.unavailable")
    payload = _plaintext(value, maximum=32_768)
    return hmac.new(
        key,
        b"metahub:enterprise-sso:" + purpose.encode("ascii") + b":" + payload,
        hashlib.sha256,
    ).hexdigest()


def encrypt_enterprise_sso_secret(
    value: object,
    *,
    secret: object,
    owner: EnterpriseSsoSecretOwner,
) -> str:
    """Encrypt one bounded secret and bind it to its exact Workspace resource owner."""

    checked = _owner(owner)
    plaintext = _plaintext(value, maximum=4096)
    nonce = token_bytes(12)
    try:
        ciphertext = AESGCM(_secret_key(secret, _ENCRYPTION_DOMAIN)).encrypt(
            nonce, plaintext, _aad(checked)
        )
    except EnterpriseSsoCryptoError:
        raise
    except Exception:
        raise EnterpriseSsoCryptoError("sso.unavailable") from None
    return ":".join(
        (
            ENTERPRISE_SSO_CIPHERTEXT_FORMAT,
            str(checked.key_version),
            checked.purpose,
            _b64(nonce),
            _b64(ciphertext),
        )
    )


def decrypt_enterprise_sso_secret(
    value: object,
    *,
    secret: object,
    owner: EnterpriseSsoSecretOwner,
) -> str:
    """Authenticate one exact owner and return plaintext only to the current call stack."""

    checked = _owner(owner)
    if type(value) is not str:
        raise EnterpriseSsoCryptoError("sso.unavailable")
    parts = value.split(":")
    if len(parts) != 5 or parts[:3] != [
        ENTERPRISE_SSO_CIPHERTEXT_FORMAT,
        str(checked.key_version),
        checked.purpose,
    ]:
        raise EnterpriseSsoCryptoError("sso.unavailable")
    try:
        nonce = _unb64(parts[3])
        ciphertext = _unb64(parts[4])
        if len(nonce) != 12 or len(ciphertext) < 17:
            raise ValueError
        plaintext = AESGCM(_secret_key(secret, _ENCRYPTION_DOMAIN)).decrypt(
            nonce, ciphertext, _aad(checked)
        )
        return _plaintext(plaintext, maximum=4096).decode("utf-8", errors="strict")
    except EnterpriseSsoCryptoError:
        raise
    except (InvalidTag, binascii.Error, UnicodeError, ValueError, TypeError):
        raise EnterpriseSsoCryptoError("sso.unavailable") from None
    except Exception:
        raise EnterpriseSsoCryptoError("sso.unavailable") from None


def _owner(owner: object) -> EnterpriseSsoSecretOwner:
    if (
        not isinstance(owner, EnterpriseSsoSecretOwner)
        or _WORKSPACE_ID.fullmatch(owner.workspace_id) is None
        or type(owner.tenant_id) is not str
        or not 3 <= len(owner.tenant_id) <= 80
        or _OWNER_ID.fullmatch(owner.resource_id) is None
        or owner.purpose not in {"oidc_client_secret", "pkce_verifier"}
        or type(owner.key_version) is not int
        or owner.key_version != 1
    ):
        raise EnterpriseSsoCryptoError("sso.unavailable")
    expected_prefix = "ssc_" if owner.purpose == "oidc_client_secret" else "sls_"
    if not owner.resource_id.startswith(expected_prefix):
        raise EnterpriseSsoCryptoError("sso.unavailable")
    return owner


def _aad(owner: EnterpriseSsoSecretOwner) -> bytes:
    return json.dumps(
        [
            ENTERPRISE_SSO_CIPHERTEXT_FORMAT,
            owner.workspace_id,
            owner.tenant_id,
            owner.resource_id,
            owner.purpose,
            owner.key_version,
        ],
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("ascii")


def _secret_key(secret: object, domain: bytes) -> bytes:
    if type(secret) is not str or secret != secret.strip():
        raise EnterpriseSsoCryptoError("sso.unavailable")
    try:
        encoded = secret.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        raise EnterpriseSsoCryptoError("sso.unavailable") from None
    if not 32 <= len(encoded) <= 512 or any(ord(character) < 33 for character in secret):
        raise EnterpriseSsoCryptoError("sso.unavailable")
    return hmac.new(encoded, domain, hashlib.sha256).digest()


def _plaintext(value: object, *, maximum: int) -> bytes:
    if type(value) is str:
        try:
            encoded = value.encode("utf-8", errors="strict")
        except UnicodeEncodeError:
            raise EnterpriseSsoCryptoError("sso.unavailable") from None
    elif type(value) is bytes:
        encoded = value
        try:
            encoded.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            raise EnterpriseSsoCryptoError("sso.unavailable") from None
    else:
        raise EnterpriseSsoCryptoError("sso.unavailable")
    if not encoded or len(encoded) > maximum:
        raise EnterpriseSsoCryptoError("sso.unavailable")
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


__all__ = [
    "ENTERPRISE_SSO_CIPHERTEXT_FORMAT",
    "EnterpriseSsoCryptoError",
    "EnterpriseSsoSecretOwner",
    "decrypt_enterprise_sso_secret",
    "encrypt_enterprise_sso_secret",
    "enterprise_sso_hmac",
]
