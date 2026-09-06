"""Purpose-bound Ed25519 verification using deployment-owned public trust."""

from __future__ import annotations

import base64
import hmac
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .models import GateCode, RequestTimestamp
from .service import EvidenceVerificationRequest, evidence_envelope_sha256

SIGNATURE_PURPOSE = b"metahub.launch-evidence.v1\0"
MAX_TRUST_BYTES = 65_536


class _TrustKey(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    key_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{1,63}$")
    gate_code: GateCode
    verifier_code: str = Field(pattern=r"^[a-z][a-z0-9_-]{4,47}$")
    verifier_version: int = Field(ge=1)
    public_key: str = Field(pattern=r"^[A-Za-z0-9_-]{43}$")
    not_before: RequestTimestamp
    not_after: RequestTimestamp

    @model_validator(mode="after")
    def validity(self):
        if self.not_before >= self.not_after:
            raise ValueError("invalid trust interval")
        _decode(self.public_key, 32)
        return self


class _TrustBundle(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal["metahub.launch-evidence-trust.v1"]
    keys: list[_TrustKey] = Field(max_length=160)

    @model_validator(mode="after")
    def identities(self):
        if len({key.key_id for key in self.keys}) != len(self.keys):
            raise ValueError("duplicate trust key identity")
        return self


def _decode(value: str, length: int) -> bytes:
    raw = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
    if len(raw) != length or base64.urlsafe_b64encode(raw).rstrip(b"=").decode() != value:
        raise ValueError("invalid canonical base64url")
    return raw


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate trust property")
        result[key] = value
    return result


def evidence_signing_bytes(request: EvidenceVerificationRequest) -> bytes:
    digest = evidence_envelope_sha256(request)
    if not hmac.compare_digest(digest, request.envelope_sha256):
        raise ValueError("evidence envelope digest mismatch")
    return SIGNATURE_PURPOSE + bytes.fromhex(digest)


@dataclass(frozen=True, slots=True)
class FileEvidenceVerifier:
    trust_path: Path = field(repr=False)

    def __call__(self, request: EvidenceVerificationRequest, signature: str) -> bool:
        with self.trust_path.open("rb") as stream:
            content = stream.read(MAX_TRUST_BYTES + 1)
        if len(content) > MAX_TRUST_BYTES:
            raise ValueError("trust bundle too large")
        bundle = _TrustBundle.model_validate(json.loads(content, object_pairs_hook=_unique_object))
        message = evidence_signing_bytes(request)
        signed = _decode(signature, 64)
        now = datetime.now(UTC)
        for key in bundle.keys:
            if (
                (key.gate_code, key.verifier_code, key.verifier_version)
                != (request.gate_code, request.verifier_code, request.verifier_version)
                or not key.not_before <= now < key.not_after
            ):
                continue
            try:
                public = Ed25519PublicKey.from_public_bytes(_decode(key.public_key, 32))
                public.verify(signed, message)
            except InvalidSignature:
                continue
            return True
        return False


def install_launch_evidence_verifier(app) -> None:
    configured = app.state.config.launch_evidence_trust_bundle_path
    app.state.launch_evidence_verifier = (
        FileEvidenceVerifier(Path(configured)) if configured else None
    )
