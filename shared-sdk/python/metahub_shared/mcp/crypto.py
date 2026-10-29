"""Owner-bound AES-GCM and tenant/domain HMAC helpers for PRD-082 MCP facts."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
from secrets import token_bytes
from typing import Final

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from .contracts import (
    MCP_CRYPTO_KINDS,
    MCP_HMAC_KINDS,
    McpContractError,
    McpCryptoOwner,
    McpHmacKind,
    McpHmacOwner,
    McpVersionedSecret,
)

MCP_CIPHERTEXT_FORMAT: Final[str] = "metahub-mcp-aesgcm-v1"
DEFAULT_MCP_ENCRYPTION_SECRET: Final[str] = (
    "dev-only-change-me-metahub-mcp-encryption-secret-2026"
)
DEFAULT_MCP_HMAC_SECRET: Final[str] = "dev-only-change-me-metahub-mcp-hmac-secret-2026"

_HKDF_SALT: Final[bytes] = b"metahub/mcp/hkdf/salt/v1"
_ENC_INFO: Final[bytes] = b"metahub/mcp/encryption/v1/"
_HMAC_INFO: Final[bytes] = b"metahub/mcp/hmac/v1/"
_MAX_PLAINTEXT = {
    "endpoint": 2_048,
    "bearer": 16_384,
    "arguments": 65_536,
    "result": 1_048_576,
}


def encrypt_mcp_value(value: object, *, secret: object, owner: McpCryptoOwner) -> str:
    """Encrypt UTF-8/bytes material with AAD bound to tenant, owner, kind, and key version."""

    checked = _crypto_owner(owner)
    plaintext = _plaintext(value, maximum=_MAX_PLAINTEXT[checked.kind])
    nonce = token_bytes(12)
    try:
        ciphertext = AESGCM(_derive_key(secret, _ENC_INFO + checked.kind.encode("ascii"))).encrypt(
            nonce,
            plaintext,
            _crypto_aad(checked),
        )
    except McpContractError:
        raise
    except Exception:
        raise McpContractError("mcp.unavailable") from None
    return ":".join(
        (
            MCP_CIPHERTEXT_FORMAT,
            str(checked.key_version),
            checked.kind,
            _b64(nonce),
            _b64(ciphertext),
        )
    )


def decrypt_mcp_value(value: object, *, secret: object, owner: McpCryptoOwner) -> bytes:
    """Authenticate one exact MCP owner and return plaintext only to the current call stack."""

    checked = _crypto_owner(owner)
    if type(value) is not str:
        raise McpContractError("mcp.unavailable")
    parts = value.split(":")
    expected = [MCP_CIPHERTEXT_FORMAT, str(checked.key_version), checked.kind]
    if len(parts) != 5 or parts[:3] != expected:
        raise McpContractError("mcp.unavailable")
    try:
        nonce = _unb64(parts[3])
        ciphertext = _unb64(parts[4])
        if len(nonce) != 12 or len(ciphertext) < 16:
            raise ValueError
        plaintext = AESGCM(
            _derive_key(secret, _ENC_INFO + checked.kind.encode("ascii"))
        ).decrypt(nonce, ciphertext, _crypto_aad(checked))
        return _plaintext(plaintext, maximum=_MAX_PLAINTEXT[checked.kind])
    except McpContractError:
        raise
    except (InvalidTag, binascii.Error, UnicodeError, ValueError, TypeError):
        raise McpContractError("mcp.unavailable") from None
    except Exception:
        raise McpContractError("mcp.unavailable") from None


def mcp_hmac(value: object, *, secret: object, owner: McpHmacOwner) -> str:
    """Return an unprefixed lowercase tenant/domain HMAC suitable for a bounded DB column."""

    checked = _hmac_owner(owner)
    plaintext = _plaintext(value, maximum=1_048_576)
    key = _derive_key(secret, _HMAC_INFO + checked.kind.encode("ascii"))
    digest = hmac.new(key, _hmac_aad(checked) + b"\x00" + plaintext, hashlib.sha256)
    return digest.hexdigest()


def mcp_hmac_candidates(
    value: object,
    *,
    keyring: tuple[McpVersionedSecret, ...],
    tenant_id: str,
    owner_id: str,
    kind: McpHmacKind,
) -> tuple[tuple[int, str], ...]:
    """Build active-first current/previous lookup candidates without changing their owners."""

    checked = validate_mcp_keyring(keyring)
    return tuple(
        (
            key.version,
            mcp_hmac(
                value,
                secret=key.secret,
                owner=McpHmacOwner(tenant_id, owner_id, kind, key.version),
            ),
        )
        for key in checked
    )


def decrypt_mcp_value_from_keyring(
    value: object,
    *,
    keyring: tuple[McpVersionedSecret, ...],
    tenant_id: str,
    owner_id: str,
    kind: str,
) -> tuple[bytes, int]:
    """Select the stored ciphertext version from an explicit current/previous keyring."""

    if type(value) is not str:
        raise McpContractError("mcp.unavailable")
    parts = value.split(":", 3)
    if len(parts) != 4 or parts[0] != MCP_CIPHERTEXT_FORMAT:
        raise McpContractError("mcp.unavailable")
    try:
        version = int(parts[1])
    except ValueError:
        raise McpContractError("mcp.unavailable") from None
    if type(kind) is not str or kind not in MCP_CRYPTO_KINDS or parts[2] != kind:
        raise McpContractError("mcp.unavailable")
    keys = {item.version: item for item in validate_mcp_keyring(keyring)}
    key = keys.get(version)
    if key is None:
        raise McpContractError("mcp.unavailable")
    plaintext = decrypt_mcp_value(
        value,
        secret=key.secret,
        owner=McpCryptoOwner(tenant_id, owner_id, kind, version),  # type: ignore[arg-type]
    )
    return plaintext, version


def validate_mcp_keyring(
    keyring: object,
    *,
    forbidden_secrets: tuple[object, ...] = (),
) -> tuple[McpVersionedSecret, ...]:
    """Validate a one- or two-key active-first ring and reject cross-domain secret reuse."""

    if type(keyring) is not tuple or not 1 <= len(keyring) <= 2:
        raise McpContractError("mcp.feature_disabled")
    if any(not isinstance(item, McpVersionedSecret) for item in keyring):
        raise McpContractError("mcp.feature_disabled")
    versions = tuple(item.version for item in keyring)
    if any(type(version) is not int or not 1 <= version <= 2_147_483_647 for version in versions):
        raise McpContractError("mcp.feature_disabled")
    if len(set(versions)) != len(versions):
        raise McpContractError("mcp.feature_disabled")
    secrets = tuple(_secret_bytes(item.secret) for item in keyring)
    if len(secrets) == 2 and hmac.compare_digest(secrets[0], secrets[1]):
        raise McpContractError("mcp.feature_disabled")
    for candidate in secrets:
        for forbidden in forbidden_secrets:
            try:
                other = _secret_bytes(forbidden)
            except McpContractError:
                continue
            if hmac.compare_digest(candidate, other):
                raise McpContractError("mcp.feature_disabled")
    return keyring


def validate_mcp_secret_domains(
    *,
    encryption_keyring: tuple[McpVersionedSecret, ...],
    hmac_keyring: tuple[McpVersionedSecret, ...],
    forbidden_secrets: tuple[object, ...] = (),
) -> None:
    """Prove encryption/HMAC roots are distinct from each other and other product domains."""

    encryption = validate_mcp_keyring(
        encryption_keyring,
        forbidden_secrets=forbidden_secrets,
    )
    hmac_keys = validate_mcp_keyring(hmac_keyring, forbidden_secrets=forbidden_secrets)
    for first in encryption:
        for second in hmac_keys:
            if hmac.compare_digest(_secret_bytes(first.secret), _secret_bytes(second.secret)):
                raise McpContractError("mcp.feature_disabled")


def _crypto_owner(owner: object) -> McpCryptoOwner:
    if not isinstance(owner, McpCryptoOwner) or owner.kind not in MCP_CRYPTO_KINDS:
        raise McpContractError("mcp.unavailable")
    _owner_dimensions(owner.tenant_id, owner.owner_id, owner.key_version)
    return owner


def _hmac_owner(owner: object) -> McpHmacOwner:
    if not isinstance(owner, McpHmacOwner) or owner.kind not in MCP_HMAC_KINDS:
        raise McpContractError("mcp.unavailable")
    _owner_dimensions(owner.tenant_id, owner.owner_id, owner.key_version)
    return owner


def _owner_dimensions(tenant_id: object, owner_id: object, key_version: object) -> None:
    if (
        type(key_version) is not int
        or not 1 <= key_version <= 2_147_483_647
        or any(
            type(value) is not str
            or not value
            or len(value) > 160
            or not value.isascii()
            or any(ord(character) < 33 or ord(character) > 126 for character in value)
            for value in (tenant_id, owner_id)
        )
    ):
        raise McpContractError("mcp.unavailable")


def _crypto_aad(owner: McpCryptoOwner) -> bytes:
    return json.dumps(
        [
            MCP_CIPHERTEXT_FORMAT,
            owner.tenant_id,
            owner.owner_id,
            owner.kind,
            owner.key_version,
        ],
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("ascii")


def _hmac_aad(owner: McpHmacOwner) -> bytes:
    # Endpoint fingerprints intentionally deduplicate across Server rows in one tenant.  Other
    # domains remain owner-bound (for example actor+operation idempotency and Invocation input).
    owner_scope = "endpoint" if owner.kind == "endpoint" else owner.owner_id
    return json.dumps(
        ["metahub-mcp-hmac-v1", owner.tenant_id, owner_scope, owner.kind, owner.key_version],
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("ascii")


def _derive_key(secret: object, info: bytes) -> bytes:
    try:
        return HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=_HKDF_SALT,
            info=info,
        ).derive(_secret_bytes(secret))
    except McpContractError:
        raise
    except Exception:
        raise McpContractError("mcp.feature_disabled") from None


def _secret_bytes(secret: object) -> bytes:
    if type(secret) is not str or secret != secret.strip():
        raise McpContractError("mcp.feature_disabled")
    try:
        encoded = secret.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        raise McpContractError("mcp.feature_disabled") from None
    if not 32 <= len(encoded) <= 4_096:
        raise McpContractError("mcp.feature_disabled")
    return encoded


def _plaintext(value: object, *, maximum: int) -> bytes:
    if type(value) is str:
        try:
            encoded = value.encode("utf-8", errors="strict")
        except UnicodeEncodeError:
            raise McpContractError("mcp.unavailable") from None
    elif type(value) is bytes:
        encoded = value
        try:
            encoded.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            raise McpContractError("mcp.unavailable") from None
    else:
        raise McpContractError("mcp.unavailable")
    if not encoded or len(encoded) > maximum:
        raise McpContractError("mcp.unavailable")
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
