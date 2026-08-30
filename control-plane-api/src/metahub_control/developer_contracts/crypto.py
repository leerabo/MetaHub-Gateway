"""KMS boundary for public contract and SDK Ed25519 signatures."""

from __future__ import annotations

import base64
import hashlib
from collections.abc import Callable
from dataclasses import dataclass

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError


@dataclass(frozen=True, slots=True)
class ExternalSignature:
    key_id: str
    public_key: bytes
    signature: bytes


ContractSigner = Callable[[bytes], ExternalSignature]


def sign_release_payload(
    config: AppConfig,
    payload: bytes,
    *,
    external_signer: ContractSigner | None = None,
) -> ExternalSignature:
    """Sign through an injected KMS adapter, with a local-only deterministic test provider."""

    if external_signer is not None:
        result = external_signer(payload)
    elif config.kms_provider == "test":
        secret = config.kms_test_master_key
        if not secret or len(secret.encode("utf-8")) < 32:
            raise ApiError("developer_contract.kms_unavailable")
        seed = hashlib.sha256(
            b"prd133/developer-contract-test-kms/v1\0" + secret.encode("utf-8")
        ).digest()
        private_key = Ed25519PrivateKey.from_private_bytes(seed)
        result = ExternalSignature(
            key_id="developer-test-v1",
            public_key=private_key.public_key().public_bytes_raw(),
            signature=private_key.sign(payload),
        )
    else:
        raise ApiError("developer_contract.kms_unavailable")
    if (
        not 8 <= len(result.key_id) <= 80
        or len(result.public_key) != 32
        or len(result.signature) != 64
    ):
        raise ApiError("developer_contract.kms_unavailable")
    try:
        Ed25519PublicKey.from_public_bytes(result.public_key).verify(result.signature, payload)
    except (ValueError, InvalidSignature):
        raise ApiError("developer_contract.kms_unavailable") from None
    return result


def unpadded_b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


__all__ = ["ContractSigner", "ExternalSignature", "sign_release_payload", "unpadded_b64"]
