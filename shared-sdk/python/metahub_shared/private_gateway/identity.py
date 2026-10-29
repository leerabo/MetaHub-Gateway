"""Strict public-key and registration-proof contracts for private gateways."""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
from dataclasses import dataclass

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

_DEPLOYMENT_RE = re.compile(r"^edp_[0-9a-f]{32}$")
_REGISTRATION_TOKEN_RE = re.compile(r"^erg_[A-Za-z0-9_-]{43}$")
_CHALLENGE_RE = re.compile(r"^ech_[A-Za-z0-9_-]{43}$")
_NONCE_RE = re.compile(r"^ecn_[A-Za-z0-9_-]{32,64}$")
_PROOF_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")
_MAX_CSR_BYTES = 8192


@dataclass(frozen=True, slots=True, repr=False)
class EdgeCsr:
    """A verified Ed25519 CSR represented only by public material and digests."""

    csr: x509.CertificateSigningRequest
    der: bytes
    csr_sha256: str
    public_key_sha256: str
    public_key: Ed25519PublicKey


def parse_edge_csr(csr_pem: str) -> EdgeCsr:
    """Parse an empty-subject, extension-free Ed25519 PKCS#10 request."""

    if type(csr_pem) is not str:
        raise ValueError("edge.csr_invalid")
    try:
        encoded = csr_pem.encode("ascii", errors="strict")
    except UnicodeEncodeError:
        raise ValueError("edge.csr_invalid") from None
    if (
        not 1 <= len(encoded) <= _MAX_CSR_BYTES
        or not csr_pem.startswith("-----BEGIN CERTIFICATE REQUEST-----\n")
        or not csr_pem.endswith("-----END CERTIFICATE REQUEST-----\n")
        or csr_pem.count("-----BEGIN CERTIFICATE REQUEST-----") != 1
        or "\r" in csr_pem
    ):
        raise ValueError("edge.csr_invalid")
    try:
        csr = x509.load_pem_x509_csr(encoded)
        public_key = csr.public_key()
        extensions = tuple(csr.extensions)
    except (TypeError, ValueError):
        raise ValueError("edge.csr_invalid") from None
    if (
        not csr.is_signature_valid
        or not isinstance(public_key, Ed25519PublicKey)
        or len(csr.subject) != 0
        or extensions
    ):
        raise ValueError("edge.csr_invalid")
    der = csr.public_bytes(serialization.Encoding.DER)
    public_bytes = public_key.public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    return EdgeCsr(
        csr=csr,
        der=der,
        csr_sha256=hashlib.sha256(der).hexdigest(),
        public_key_sha256=hashlib.sha256(public_bytes).hexdigest(),
        public_key=public_key,
    )


def build_registration_challenge_proof(
    registration_token: str,
    *,
    deployment_id: str,
    challenge: str,
    csr_sha256: str,
) -> str:
    """Bind one registration credential to one server challenge and CSR."""

    token = validate_edge_registration_token(registration_token)
    checked_deployment = validate_edge_deployment_id(deployment_id)
    checked_challenge = validate_edge_challenge(challenge)
    if not re.fullmatch(r"[0-9a-f]{64}", csr_sha256):
        raise ValueError("edge.csr_invalid")
    message = (
        "metahub/edge-registration-proof/v1\0"
        f"{checked_deployment}\0{checked_challenge}\0{csr_sha256}"
    ).encode("ascii")
    digest = hmac.new(token.encode("ascii"), message, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def verify_registration_challenge_proof(
    proof: str,
    registration_token: str,
    *,
    deployment_id: str,
    challenge: str,
    csr_sha256: str,
) -> bool:
    """Compare a bounded proof without exposing which registration fact failed."""

    if type(proof) is not str or _PROOF_RE.fullmatch(proof) is None:
        return False
    try:
        expected = build_registration_challenge_proof(
            registration_token,
            deployment_id=deployment_id,
            challenge=challenge,
            csr_sha256=csr_sha256,
        )
    except ValueError:
        return False
    return hmac.compare_digest(proof, expected)


def certificate_sha256(certificate: x509.Certificate | bytes) -> str:
    """Return the TLS binding digest over the leaf certificate DER bytes."""

    der = (
        certificate.public_bytes(serialization.Encoding.DER)
        if isinstance(certificate, x509.Certificate)
        else certificate
    )
    if type(der) is not bytes or not der:
        raise ValueError("edge.certificate_invalid")
    return hashlib.sha256(der).hexdigest()


def validate_edge_registration_token(value: str) -> str:
    if type(value) is not str or _REGISTRATION_TOKEN_RE.fullmatch(value) is None:
        raise ValueError("edge.registration_invalid")
    return value


def validate_edge_challenge(value: str) -> str:
    if type(value) is not str or _CHALLENGE_RE.fullmatch(value) is None:
        raise ValueError("edge.registration_invalid")
    return value


def validate_edge_deployment_id(value: str) -> str:
    if type(value) is not str or _DEPLOYMENT_RE.fullmatch(value) is None:
        raise ValueError("edge.deployment_not_found")
    return value


def validate_edge_nonce(value: str) -> str:
    if type(value) is not str or _NONCE_RE.fullmatch(value) is None:
        raise ValueError("edge.authentication_failed")
    return value
