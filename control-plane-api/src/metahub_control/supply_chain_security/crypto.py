"""Purpose-separated KMS boundary for artifact attestation signatures."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError


@dataclass(frozen=True, slots=True)
class AttestationSignature:
    key_id: str
    public_key: bytes
    signature: bytes


AttestationSigner = Callable[[bytes], AttestationSignature]


def sign_attestation_payload(
    config: AppConfig,
    payload: bytes,
    *,
    external_signer: AttestationSigner | None = None,
) -> AttestationSignature:
    if external_signer is not None:
        result = external_signer(payload)
    elif config.kms_provider == "test":
        secret = config.kms_test_master_key
        if not secret or len(secret.encode("utf-8")) < 32:
            raise ApiError("supply_chain.kms_unavailable")
        seed = hashlib.sha256(
            b"prd136/supply-chain-test-kms/v1\0" + secret.encode("utf-8")
        ).digest()
        private_key = Ed25519PrivateKey.from_private_bytes(seed)
        result = AttestationSignature(
            key_id="supply-chain-test-v1",
            public_key=private_key.public_key().public_bytes_raw(),
            signature=private_key.sign(payload),
        )
    else:
        raise ApiError("supply_chain.kms_unavailable")
    if (
        not 8 <= len(result.key_id) <= 80
        or len(result.public_key) != 32
        or len(result.signature) != 64
    ):
        raise ApiError("supply_chain.kms_unavailable")
    try:
        Ed25519PublicKey.from_public_bytes(result.public_key).verify(result.signature, payload)
    except (InvalidSignature, ValueError):
        raise ApiError("supply_chain.kms_unavailable") from None
    return result


__all__ = ["AttestationSignature", "AttestationSigner", "sign_attestation_payload"]
