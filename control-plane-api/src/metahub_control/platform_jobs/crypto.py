"""KMS envelope encryption for platform job inputs and checkpoints."""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass
from typing import Literal

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError


@dataclass(frozen=True, slots=True, repr=False)
class PreparedEnvelope:
    envelope: str
    plaintext_sha256: str


def encrypt_job_value(
    config: AppConfig,
    value: object,
    *,
    kind: Literal["input", "checkpoint"],
    owner_id: str,
    type_code: str,
) -> PreparedEnvelope:
    plaintext = json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("ascii")
    digest = hashlib.sha256(plaintext).hexdigest()
    aad = _aad(kind, owner_id, type_code, digest)
    key = bytearray()
    client = None
    try:
        if config.kms_provider == "test":
            secret = config.kms_test_master_key
            if not secret or len(secret.encode("utf-8")) < 32:
                raise ValueError
            key.extend(
                hashlib.sha256(
                    b"prd106/test-kms/v1\0" + secret.encode("utf-8")
                ).digest()
            )
            nonce = _nonce()
            ciphertext = AESGCM(bytes(key)).encrypt(nonce, plaintext, aad)
            document = {
                "alg": "A256GCM",
                "ciphertext": _b64(ciphertext),
                "key": "test-v1",
                "nonce": _b64(nonce),
                "provider": "test",
                "v": 1,
            }
        elif config.kms_provider == "aws" and config.kms_key_id:
            client = _aws_client(config)
            response = client.generate_data_key(
                KeyId=config.kms_key_id,
                KeySpec="AES_256",
                EncryptionContext={"metahub-purpose": "platform-job-v1"},
            )
            key.extend(bytes(response["Plaintext"]))
            nonce = _nonce()
            ciphertext = AESGCM(bytes(key)).encrypt(nonce, plaintext, aad)
            document = {
                "alg": "A256GCM",
                "ciphertext": _b64(ciphertext),
                "encrypted_key": _b64(bytes(response["CiphertextBlob"])),
                "key": "aws-kms-v1",
                "nonce": _b64(nonce),
                "provider": "aws",
                "v": 1,
            }
        else:
            raise ValueError
        return PreparedEnvelope(
            envelope=json.dumps(document, ensure_ascii=True, sort_keys=True, separators=(",", ":")),
            plaintext_sha256=digest,
        )
    except Exception:
        raise ApiError("platform_job.kms_unavailable") from None
    finally:
        _zero(key)
        if client is not None:
            try:
                client.close()
            except Exception:
                pass


def decrypt_job_value(
    config: AppConfig,
    envelope: str,
    *,
    expected_sha256: str,
    kind: Literal["input", "checkpoint"],
    owner_id: str,
    type_code: str,
) -> object:
    try:
        document = json.loads(envelope)
        allowed = {"alg", "ciphertext", "key", "nonce", "provider", "v"}
        if document.get("provider") == "aws":
            allowed.add("encrypted_key")
        if (
            not isinstance(document, dict)
            or set(document) != allowed
            or document.get("alg") != "A256GCM"
            or document.get("v") != 1
        ):
            raise ValueError
        provider = document["provider"]
        if provider not in {"test", "aws"}:
            raise ValueError
        if provider == "test" and document["key"] != "test-v1":
            raise ValueError
        if provider == "aws" and document["key"] != "aws-kms-v1":
            raise ValueError
    except (json.JSONDecodeError, TypeError, KeyError, ValueError):
        raise ApiError("platform_job.input_invalid") from None

    key = bytearray()
    client = None
    try:
        if provider == "test":
            secret = config.kms_test_master_key
            if config.kms_provider != "test" or not secret:
                raise ApiError("platform_job.kms_unavailable")
            key.extend(
                hashlib.sha256(
                    b"prd106/test-kms/v1\0" + secret.encode("utf-8")
                ).digest()
            )
        else:
            if config.kms_provider != "aws" or not config.kms_key_id:
                raise ApiError("platform_job.kms_unavailable")
            try:
                encrypted_key = _unb64(document["encrypted_key"])
            except (KeyError, TypeError, ValueError):
                raise ApiError("platform_job.input_invalid") from None
            try:
                client = _aws_client(config)
                response = client.decrypt(
                    CiphertextBlob=encrypted_key,
                    EncryptionContext={"metahub-purpose": "platform-job-v1"},
                )
                key.extend(bytes(response["Plaintext"]))
            except (ApiError, ValueError):
                raise
            except Exception:
                raise ApiError("platform_job.kms_unavailable") from None
        try:
            plaintext = AESGCM(bytes(key)).decrypt(
                _unb64(document["nonce"]),
                _unb64(document["ciphertext"]),
                _aad(kind, owner_id, type_code, expected_sha256),
            )
        except Exception:
            raise ApiError("platform_job.input_invalid") from None
    finally:
        _zero(key)
        if client is not None:
            try:
                client.close()
            except Exception:
                pass
    try:
        if hashlib.sha256(plaintext).hexdigest() != expected_sha256:
            raise ValueError
        parsed = json.loads(plaintext.decode("ascii"))
        canonical = json.dumps(
            parsed, ensure_ascii=True, sort_keys=True, separators=(",", ":")
        ).encode("ascii")
        if canonical != plaintext:
            raise ValueError
        return parsed
    except (json.JSONDecodeError, UnicodeError, TypeError, ValueError):
        raise ApiError("platform_job.input_invalid") from None


def _aws_client(config: AppConfig):
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


def _aad(kind: str, owner_id: str, type_code: str, digest: str) -> bytes:
    return f"prd106/{kind}/v1\0{owner_id}\0{type_code}\0{digest}".encode("ascii")


def _nonce() -> bytes:
    import secrets

    return secrets.token_bytes(12)


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _unb64(value: object) -> bytes:
    if not isinstance(value, str) or not value or "=" in value:
        raise ValueError
    return base64.b64decode(
        value + "=" * ((4 - len(value) % 4) % 4), altchars=b"-_", validate=True
    )


def _zero(value: bytearray) -> None:
    value[:] = b"\0" * len(value)


__all__ = ["PreparedEnvelope", "decrypt_job_value", "encrypt_job_value"]
