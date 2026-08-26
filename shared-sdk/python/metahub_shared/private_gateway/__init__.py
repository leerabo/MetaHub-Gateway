"""Shared, secret-safe contracts for customer-owned MetaHub gateways."""

from .desired_state import (
    EdgeDesiredStateDecision,
    EdgeDesiredStateError,
    EdgeDesiredStateStore,
    VerifiedDesiredState,
    VerifiedTrustManifest,
    public_key_b64,
    sign_desired_state,
    sign_trust_manifest,
    strict_json_object,
    verify_desired_state,
    verify_trust_manifest,
)
from .desired_state import (
    canonical_json_bytes as canonical_edge_json_bytes,
)
from .identity import (
    EdgeCsr,
    build_registration_challenge_proof,
    certificate_sha256,
    parse_edge_csr,
    validate_edge_challenge,
    validate_edge_deployment_id,
    validate_edge_nonce,
    validate_edge_registration_token,
    verify_registration_challenge_proof,
)

__all__ = (
    "EdgeCsr",
    "EdgeDesiredStateDecision",
    "EdgeDesiredStateError",
    "EdgeDesiredStateStore",
    "VerifiedDesiredState",
    "VerifiedTrustManifest",
    "build_registration_challenge_proof",
    "canonical_edge_json_bytes",
    "certificate_sha256",
    "parse_edge_csr",
    "public_key_b64",
    "sign_desired_state",
    "sign_trust_manifest",
    "strict_json_object",
    "validate_edge_challenge",
    "validate_edge_deployment_id",
    "validate_edge_nonce",
    "validate_edge_registration_token",
    "verify_desired_state",
    "verify_registration_challenge_proof",
    "verify_trust_manifest",
)
