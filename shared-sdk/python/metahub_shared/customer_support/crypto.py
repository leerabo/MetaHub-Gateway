"""Owner-bound AES-GCM and HMAC contracts for PRD-132 support content."""

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

SupportObjectKind = Literal["ticket", "message", "survey"]
SupportFieldKind = Literal["subject", "body", "comment"]
SupportReferenceKind = Literal[
    "request",
    "incident",
    "edge_deployment",
    "migration",
    "uploaded_file",
]

SUPPORT_CONTENT_FORMAT = "metahub-support-content-aesgcm-v1"
_KEY_DOMAIN = b"metahub/prd132/customer-support/content/aesgcm/v1"
_REFERENCE_DOMAIN = b"metahub/prd132/customer-support/reference/hmac/v1"
_OWNER_ID = re.compile(r"^(?:wsp|tkt|msg|ssv)_[0-9a-f]{32}$")
_TENANT_ID = re.compile(r"^[A-Za-z0-9._:-]{3,128}$")
_REFERENCE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{2,255}$")
_MAX_CONTENT_BYTES = {"subject": 512, "body": 16_384, "comment": 4_096}


class SupportContentCryptoError(ValueError):
    """Fixed crypto failure that must map to support.unavailable."""


@dataclass(frozen=True, slots=True)
class SupportContentOwner:
    workspace_id: str
    tenant_id: str
    object_kind: SupportObjectKind
    object_id: str
    field_kind: SupportFieldKind
    key_version: int = 1


def encrypt_support_content(value: object, *, secret: object, owner: SupportContentOwner) -> str:
    checked = _owner(owner)
    plaintext = _plaintext(value, checked.field_kind)
    nonce = token_bytes(12)
    try:
        ciphertext = AESGCM(_key(secret, checked.field_kind)).encrypt(
            nonce, plaintext, _aad(checked)
        )
    except SupportContentCryptoError:
        raise
    except Exception:
        raise SupportContentCryptoError("support.unavailable") from None
    return ":".join(
        (
            SUPPORT_CONTENT_FORMAT,
            str(checked.key_version),
            checked.object_kind,
            checked.field_kind,
            _b64(nonce),
            _b64(ciphertext),
        )
    )


def decrypt_support_content(value: object, *, secret: object, owner: SupportContentOwner) -> str:
    checked = _owner(owner)
    if type(value) is not str:
        raise SupportContentCryptoError("support.unavailable")
    parts = value.split(":")
    expected = [
        SUPPORT_CONTENT_FORMAT,
        str(checked.key_version),
        checked.object_kind,
        checked.field_kind,
    ]
    if len(parts) != 6 or parts[:4] != expected:
        raise SupportContentCryptoError("support.unavailable")
    try:
        nonce = _unb64(parts[4])
        ciphertext = _unb64(parts[5])
        if len(nonce) != 12 or len(ciphertext) < 17:
            raise ValueError
        plaintext = AESGCM(_key(secret, checked.field_kind)).decrypt(
            nonce, ciphertext, _aad(checked)
        )
        return _plaintext(plaintext, checked.field_kind).decode("utf-8", errors="strict")
    except SupportContentCryptoError:
        raise
    except (InvalidTag, binascii.Error, UnicodeError, ValueError, TypeError):
        raise SupportContentCryptoError("support.unavailable") from None
    except Exception:
        raise SupportContentCryptoError("support.unavailable") from None


def support_reference_hmac(
    value: object,
    *,
    secret: object,
    workspace_id: object,
    tenant_id: object,
    reference_kind: SupportReferenceKind,
) -> str:
    if (
        type(workspace_id) is not str
        or re.fullmatch(r"^wsp_[0-9a-f]{32}$", workspace_id) is None
        or type(tenant_id) is not str
        or _TENANT_ID.fullmatch(tenant_id) is None
        or reference_kind
        not in {"request", "incident", "edge_deployment", "migration", "uploaded_file"}
        or type(value) is not str
        or value != value.strip()
        or _REFERENCE.fullmatch(value) is None
    ):
        raise SupportContentCryptoError("support.unavailable")
    payload = json.dumps(
        [SUPPORT_CONTENT_FORMAT, workspace_id, tenant_id, reference_kind, value],
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("ascii")
    digest = hmac.new(_key(secret, f"reference:{reference_kind}"), payload, hashlib.sha256)
    return f"hmac-sha256:{digest.hexdigest()}"


def _owner(owner: object) -> SupportContentOwner:
    expected = {
        "ticket": ("tkt_", {"subject"}),
        "message": ("msg_", {"body"}),
        "survey": ("ssv_", {"comment"}),
    }
    if not isinstance(owner, SupportContentOwner) or owner.object_kind not in expected:
        raise SupportContentCryptoError("support.unavailable")
    prefix, fields = expected[owner.object_kind]
    if (
        re.fullmatch(r"^wsp_[0-9a-f]{32}$", owner.workspace_id) is None
        or _TENANT_ID.fullmatch(owner.tenant_id) is None
        or _OWNER_ID.fullmatch(owner.object_id) is None
        or not owner.object_id.startswith(prefix)
        or owner.field_kind not in fields
        or type(owner.key_version) is not int
        or owner.key_version != 1
    ):
        raise SupportContentCryptoError("support.unavailable")
    return owner


def _aad(owner: SupportContentOwner) -> bytes:
    return json.dumps(
        [
            SUPPORT_CONTENT_FORMAT,
            owner.workspace_id,
            owner.tenant_id,
            owner.object_kind,
            owner.object_id,
            owner.field_kind,
            owner.key_version,
        ],
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("ascii")


def _key(secret: object, domain: str) -> bytes:
    if type(secret) is not str or secret != secret.strip():
        raise SupportContentCryptoError("support.unavailable")
    try:
        encoded = secret.encode("utf-8", errors="strict")
        label = domain.encode("ascii", errors="strict")
    except (UnicodeEncodeError, AttributeError):
        raise SupportContentCryptoError("support.unavailable") from None
    if not 32 <= len(encoded) <= 4096 or any(ord(character) < 33 for character in secret):
        raise SupportContentCryptoError("support.unavailable")
    return hmac.new(encoded, _KEY_DOMAIN + b"\x00" + label, hashlib.sha256).digest()


def _plaintext(value: object, field_kind: SupportFieldKind) -> bytes:
    if type(value) is str:
        try:
            encoded = value.encode("utf-8", errors="strict")
        except UnicodeEncodeError:
            raise SupportContentCryptoError("support.unavailable") from None
    elif type(value) is bytes:
        encoded = value
        try:
            encoded.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            raise SupportContentCryptoError("support.unavailable") from None
    else:
        raise SupportContentCryptoError("support.unavailable")
    if not encoded or len(encoded) > _MAX_CONTENT_BYTES[field_kind]:
        raise SupportContentCryptoError("support.unavailable")
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
