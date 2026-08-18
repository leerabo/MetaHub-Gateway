"""PRD-089 owner-bound AES-GCM。

导出 ZIP 和注销留存分段共享部署根密钥，但使用不同固定派生域；明文仅存在于当前调用栈，异常、
``repr`` 和持久化字段都不能包含明文、密钥或认证失败细节。
"""

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

TenantDataKind = Literal["export", "archive"]

_FORMAT = "metahub-tenant-data-aesgcm-v1"
_EXPORT_DOMAIN = b"metahub/prd089/tenant-data/export/aesgcm/v1"
_ARCHIVE_DOMAIN = b"metahub/prd089/tenant-data/archive/aesgcm/v1"
# Version identifiers intentionally include a slash (for example
# ``metahub.tenant-export/v1``); every owner field is still bounded ASCII.
_OWNER_ID = re.compile(r"^[A-Za-z0-9_.:/\-]{1,200}$")
_MAX_PLAINTEXT_BYTES = 104_857_600


class TenantDataCryptoError(ValueError):
    """固定密码学失败；调用方只能映射为统一 tenant-data 错误码。"""


@dataclass(frozen=True, slots=True)
class TenantDataEnvelopeOwner:
    """把密文绑定到租户、业务对象、分段和 schema，阻止跨 owner 调包。"""

    tenant_id: str
    object_id: str
    segment_id: str
    schema_version: str
    key_version: int = 1


def encrypt_tenant_data(
    value: bytes, *, secret: str, owner: TenantDataEnvelopeOwner, kind: TenantDataKind
) -> str:
    """加密一个有界 ZIP 或压缩归档分段，只返回 ASCII 认证密文。"""

    plaintext = _plaintext(value)
    aad = _aad(owner, kind=kind)
    nonce = token_bytes(12)
    ciphertext = AESGCM(_key(secret, kind=kind)).encrypt(nonce, plaintext, aad)
    return ":".join((_FORMAT, str(owner.key_version), kind, _b64(nonce), _b64(ciphertext)))


def decrypt_tenant_data(
    value: str, *, secret: str, owner: TenantDataEnvelopeOwner, kind: TenantDataKind
) -> bytes:
    """在内存中认证并解密 owner-bound 密文；格式、owner 和 tag 错误保持同形。"""

    if type(value) is not str:
        raise TenantDataCryptoError("tenant_data.crypto_unavailable")
    parts = value.split(":")
    if (
        len(parts) != 5
        or parts[0] != _FORMAT
        or parts[1] != str(owner.key_version)
        or parts[2] != kind
    ):
        raise TenantDataCryptoError("tenant_data.crypto_unavailable")
    try:
        nonce = _unb64(parts[3])
        ciphertext = _unb64(parts[4])
        if len(nonce) != 12 or len(ciphertext) < 17:
            raise ValueError
        plaintext = AESGCM(_key(secret, kind=kind)).decrypt(
            nonce, ciphertext, _aad(owner, kind=kind)
        )
        return _plaintext(plaintext)
    except (InvalidTag, binascii.Error, UnicodeEncodeError, ValueError, TypeError):
        # 不区分 tag、Base64、owner 或密钥错误，避免形成密码学 oracle。
        raise TenantDataCryptoError("tenant_data.crypto_unavailable") from None


def _plaintext(value: object) -> bytes:
    if type(value) is not bytes or not value or len(value) > _MAX_PLAINTEXT_BYTES:
        raise TenantDataCryptoError("tenant_data.crypto_unavailable")
    return value


def _aad(owner: TenantDataEnvelopeOwner, *, kind: TenantDataKind) -> bytes:
    if (
        not isinstance(owner, TenantDataEnvelopeOwner)
        or type(owner.key_version) is not int
        or owner.key_version < 1
        or kind not in {"export", "archive"}
        or any(
            type(value) is not str or _OWNER_ID.fullmatch(value) is None
            for value in (
                owner.tenant_id,
                owner.object_id,
                owner.segment_id,
                owner.schema_version,
            )
        )
    ):
        raise TenantDataCryptoError("tenant_data.crypto_unavailable")
    return json.dumps(
        [
            "metahub.tenant-data/v1",
            kind,
            owner.tenant_id,
            owner.object_id,
            owner.segment_id,
            owner.schema_version,
            owner.key_version,
        ],
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("ascii")


def _key(secret: object, *, kind: TenantDataKind) -> bytes:
    if type(secret) is not str:
        raise TenantDataCryptoError("tenant_data.crypto_unavailable")
    try:
        raw = secret.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        raise TenantDataCryptoError("tenant_data.crypto_unavailable") from None
    if not 32 <= len(raw) <= 512 or any(
        character.isspace() or ord(character) < 32 for character in secret
    ):
        raise TenantDataCryptoError("tenant_data.crypto_unavailable")
    domain = _EXPORT_DOMAIN if kind == "export" else _ARCHIVE_DOMAIN
    return hmac.new(raw, domain, hashlib.sha256).digest()


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii")


def _unb64(value: str) -> bytes:
    if type(value) is not str or not value:
        raise binascii.Error
    decoded = base64.b64decode(value.encode("ascii"), altchars=b"-_", validate=True)
    if _b64(decoded) != value:
        raise binascii.Error
    return decoded
