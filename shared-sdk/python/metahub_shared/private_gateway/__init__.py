"""Shared, secret-safe contracts for customer-owned MetaHub gateways."""

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
    "build_registration_challenge_proof",
    "certificate_sha256",
    "parse_edge_csr",
    "validate_edge_challenge",
    "validate_edge_deployment_id",
    "validate_edge_nonce",
    "validate_edge_registration_token",
    "verify_registration_challenge_proof",
)
