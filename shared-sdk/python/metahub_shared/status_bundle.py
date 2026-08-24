"""Strict public status bundle verification shared by Control and the read plane."""

from __future__ import annotations

import base64
import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from metahub_shared.config_distribution import canonical_json_bytes

_HASH = re.compile(r"^[0-9a-f]{64}$")
_KEY_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{2,127}$")
_B64 = re.compile(r"^[A-Za-z0-9_-]{40,2048}$")
_ENVELOPE_KEYS = frozenset(
    {
        "wire_schema_version",
        "generation",
        "published_at",
        "expires_at",
        "payload_sha256",
        "signing_key_version",
        "signature_algorithm",
        "signature",
        "payload",
    }
)
_SIGNED_KEYS = frozenset(
    {
        "wire_schema_version",
        "generation",
        "published_at",
        "expires_at",
        "payload_sha256",
        "payload",
    }
)
_FORBIDDEN_PUBLIC_KEYS = frozenset(
    {
        "account_id",
        "authorization",
        "contact",
        "email",
        "endpoint",
        "internal",
        "password",
        "prompt",
        "provider_secret",
        "response",
        "secret",
        "tenant_id",
        "token",
        "workspace_id",
    }
)


class StatusBundleError(ValueError):
    """Fixed-code failure that never includes bundle or infrastructure details."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class VerifiedStatusBundle:
    generation: int
    published_at: datetime
    expires_at: datetime
    payload_sha256: str
    payload: Mapping[str, object]


def signed_status_document(
    *,
    generation: int,
    published_at: str,
    expires_at: str,
    payload: Mapping[str, object],
) -> dict[str, object]:
    if type(generation) is not int or generation < 1 or type(payload) is not dict:
        raise StatusBundleError("status.bundle_invalid")
    _time(published_at)
    _time(expires_at)
    _reject_private_keys(payload)
    return {
        "wire_schema_version": 1,
        "generation": generation,
        "published_at": published_at,
        "expires_at": expires_at,
        "payload_sha256": hashlib.sha256(canonical_json_bytes(payload)).hexdigest(),
        "payload": payload,
    }


def build_status_envelope(
    signed: Mapping[str, object],
    *,
    algorithm: str,
    key_version: str,
    signature: str,
) -> dict[str, object]:
    if (
        type(signed) is not dict
        or frozenset(signed) != _SIGNED_KEYS
        or algorithm not in {"ed25519", "rsassa_pss_sha256"}
        or _KEY_VERSION.fullmatch(key_version) is None
        or _B64.fullmatch(signature) is None
    ):
        raise StatusBundleError("status.bundle_invalid")
    return {
        **signed,
        "signing_key_version": key_version,
        "signature_algorithm": algorithm,
        "signature": signature,
    }


def verify_status_envelope(
    value: object,
    *,
    trusted_keys: Mapping[str, bytes],
    now: datetime | None = None,
) -> VerifiedStatusBundle:
    try:
        if type(value) is not dict or frozenset(value) != _ENVELOPE_KEYS:
            raise ValueError
        signed = {key: value[key] for key in _SIGNED_KEYS}
        if (
            value["wire_schema_version"] != 1
            or type(value["generation"]) is not int
            or value["generation"] < 1
            or type(value["payload"]) is not dict
            or type(value["payload_sha256"]) is not str
            or _HASH.fullmatch(value["payload_sha256"]) is None
            or type(value["signing_key_version"]) is not str
            or _KEY_VERSION.fullmatch(value["signing_key_version"]) is None
            or type(value["signature"]) is not str
            or _B64.fullmatch(value["signature"]) is None
        ):
            raise ValueError
        payload = value["payload"]
        _reject_private_keys(payload)
        if hashlib.sha256(canonical_json_bytes(payload)).hexdigest() != value["payload_sha256"]:
            raise ValueError
        published_at = _time(value["published_at"])
        expires_at = _time(value["expires_at"])
        current = (now or datetime.now(UTC)).astimezone(UTC)
        if expires_at <= published_at or expires_at <= current:
            raise StatusBundleError("status.bundle_expired")
        public_key = trusted_keys.get(value["signing_key_version"])
        if public_key is None:
            raise ValueError
        signature = _unb64(value["signature"])
        document = canonical_json_bytes(signed)
        algorithm = value["signature_algorithm"]
        if algorithm == "ed25519":
            Ed25519PublicKey.from_public_bytes(public_key).verify(signature, document)
        elif algorithm == "rsassa_pss_sha256":
            loaded = serialization.load_der_public_key(public_key)
            if not isinstance(loaded, rsa.RSAPublicKey):
                raise ValueError
            loaded.verify(
                signature,
                document,
                padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=32),
                hashes.SHA256(),
            )
        else:
            raise ValueError
        return VerifiedStatusBundle(
            generation=value["generation"],
            published_at=published_at,
            expires_at=expires_at,
            payload_sha256=value["payload_sha256"],
            payload=payload,
        )
    except StatusBundleError:
        raise
    except Exception:
        raise StatusBundleError("status.bundle_invalid") from None


def test_kms_status_public_key(secret: str) -> bytes:
    if type(secret) is not str or len(secret.encode("utf-8")) < 32:
        raise StatusBundleError("status.bundle_invalid")
    seed = hashlib.sha256(b"prd108/test-kms-signing/v1\0" + secret.encode("utf-8")).digest()
    return Ed25519PrivateKey.from_private_bytes(seed).public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )


def parse_status_envelope(raw: bytes) -> dict[str, object]:
    if not raw or len(raw) > 1_048_576:
        raise StatusBundleError("status.bundle_invalid")

    def unique(items: list[tuple[str, object]]) -> dict[str, object]:
        parsed: dict[str, object] = {}
        for key, item in items:
            if key in parsed:
                raise ValueError
            parsed[key] = item
        return parsed

    try:
        value = json.loads(raw.decode("ascii"), object_pairs_hook=unique)
        if type(value) is not dict or canonical_json_bytes(value) != raw:
            raise ValueError
        return value
    except Exception:
        raise StatusBundleError("status.bundle_invalid") from None


def _reject_private_keys(value: object) -> None:
    if type(value) is dict:
        for key, item in value.items():
            if type(key) is not str or key.lower() in _FORBIDDEN_PUBLIC_KEYS:
                raise StatusBundleError("status.bundle_invalid")
            _reject_private_keys(item)
    elif type(value) is list:
        for item in value:
            _reject_private_keys(item)
    elif type(value) not in {str, int, bool, type(None)}:
        raise StatusBundleError("status.bundle_invalid")


def _time(value: object) -> datetime:
    if type(value) is not str or len(value) > 40:
        raise ValueError
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError
    return parsed.astimezone(UTC)


def _unb64(value: str) -> bytes:
    if "=" in value:
        raise ValueError
    return base64.b64decode(
        value + "=" * ((4 - len(value) % 4) % 4), altchars=b"-_", validate=True
    )


__all__ = [
    "StatusBundleError",
    "VerifiedStatusBundle",
    "build_status_envelope",
    "parse_status_envelope",
    "signed_status_document",
    "test_kms_status_public_key",
    "verify_status_envelope",
]
