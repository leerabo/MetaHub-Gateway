"""Envelope encryption and KMS-backed asymmetric signatures for PRD-108 manifests."""

from __future__ import annotations

import base64
import hashlib
import json
import os
from dataclasses import dataclass

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from metahub_shared.config_distribution import canonical_json_bytes
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError


@dataclass(frozen=True, slots=True, repr=False)
class SealedConfigValue:
    envelope: str
    plaintext_sha256: str


@dataclass(frozen=True, slots=True, repr=False)
class ConfigSignature:
    algorithm: str
    key_version: str
    value: str


def seal_config_value(
    config: AppConfig,
    value: object,
    *,
    owner_id: str,
    workspace_id: str,
    tenant_id: str,
    kind: str,
) -> SealedConfigValue:
    plaintext = canonical_json_bytes(value)
    digest = hashlib.sha256(plaintext).hexdigest()
    key = _encryption_key(config)
    nonce = os.urandom(12)
    ciphertext = AESGCM(key).encrypt(
        nonce, plaintext, _aad(owner_id, workspace_id, tenant_id, kind, digest)
    )
    document = {
        "algorithm": "A256GCM",
        "ciphertext": _b64(ciphertext),
        "key_version": config.tenant_data_encryption_version,
        "nonce": _b64(nonce),
        "version": 1,
    }
    return SealedConfigValue(
        envelope=json.dumps(document, ensure_ascii=True, sort_keys=True, separators=(",", ":")),
        plaintext_sha256=digest,
    )


def open_config_value(
    config: AppConfig,
    envelope: str,
    *,
    expected_sha256: str,
    owner_id: str,
    workspace_id: str,
    tenant_id: str,
    kind: str,
) -> object:
    try:
        document = json.loads(envelope)
        if (
            type(document) is not dict
            or set(document) != {"algorithm", "ciphertext", "key_version", "nonce", "version"}
            or document["algorithm"] != "A256GCM"
            or document["version"] != 1
            or document["key_version"] != config.tenant_data_encryption_version
        ):
            raise ValueError
        plaintext = AESGCM(_encryption_key(config)).decrypt(
            _unb64(document["nonce"]),
            _unb64(document["ciphertext"]),
            _aad(owner_id, workspace_id, tenant_id, kind, expected_sha256),
        )
        if hashlib.sha256(plaintext).hexdigest() != expected_sha256:
            raise ValueError
        parsed = json.loads(plaintext.decode("ascii"))
        if canonical_json_bytes(parsed) != plaintext:
            raise ValueError
        return parsed
    except ApiError:
        raise
    except Exception:
        raise ApiError("config_distribution.envelope_invalid") from None


def sign_config_manifest(config: AppConfig, manifest: object) -> ConfigSignature:
    payload = canonical_json_bytes(manifest)
    if config.kms_provider == "test":
        secret = config.kms_test_master_key
        if not secret or len(secret.encode("utf-8")) < 32:
            raise ApiError("config_distribution.kms_unavailable")
        seed = hashlib.sha256(
            b"prd108/test-kms-signing/v1\0" + secret.encode("utf-8")
        ).digest()
        signature = Ed25519PrivateKey.from_private_bytes(seed).sign(payload)
        return ConfigSignature("ed25519", "test-v1", _b64(signature))
    if config.kms_provider != "aws" or not config.config_distribution_signing_key_id:
        raise ApiError("config_distribution.kms_unavailable")
    client = None
    try:
        client = _aws_kms_client(config)
        result = client.sign(
            KeyId=config.config_distribution_signing_key_id,
            Message=payload,
            MessageType="RAW",
            SigningAlgorithm="RSASSA_PSS_SHA_256",
        )
        signature = bytes(result["Signature"])
        key_id = str(result.get("KeyId") or config.config_distribution_signing_key_id)
        if not signature:
            raise ValueError
        return ConfigSignature(
            "rsassa_pss_sha256",
            "kmsv_" + hashlib.sha256(key_id.encode("utf-8")).hexdigest()[:24],
            _b64(signature),
        )
    except ApiError:
        raise
    except Exception:
        raise ApiError("config_distribution.kms_unavailable") from None
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:
                pass


def _encryption_key(config: AppConfig) -> bytes:
    secret = config.tenant_data_encryption_secret
    if not secret or len(secret.encode("utf-8")) < 32:
        raise ApiError("config_distribution.kms_unavailable")
    return hashlib.sha256(
        b"prd108/config-envelope/v1\0"
        + str(config.tenant_data_encryption_version).encode("ascii")
        + b"\0"
        + secret.encode("utf-8")
    ).digest()


def _aws_kms_client(config: AppConfig):
    import boto3
    from botocore.config import Config

    return boto3.client(
        "kms",
        region_name=config.object_store_region,
        config=Config(
            connect_timeout=config.infrastructure_probe_timeout_seconds,
            read_timeout=config.infrastructure_probe_timeout_seconds,
            retries={"max_attempts": 1, "mode": "standard"},
        ),
    )


def _aad(
    owner_id: str, workspace_id: str, tenant_id: str, kind: str, digest: str
) -> bytes:
    return (
        f"prd108/config-envelope/v1\0{kind}\0{owner_id}\0{workspace_id}\0{tenant_id}\0{digest}"
    ).encode("ascii")


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _unb64(value: object) -> bytes:
    if not isinstance(value, str) or not value or "=" in value:
        raise ValueError
    return base64.b64decode(
        value + "=" * ((4 - len(value) % 4) % 4), altchars=b"-_", validate=True
    )


__all__ = [
    "ConfigSignature",
    "SealedConfigValue",
    "open_config_value",
    "seal_config_value",
    "sign_config_manifest",
]
