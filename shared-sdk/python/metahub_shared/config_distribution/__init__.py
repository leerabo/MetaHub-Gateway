"""Verified tenant configuration distribution contracts and local snapshot ownership."""

from .runtime import (
    ConfigDecision,
    ConfigDistributionError,
    ConfigSnapshotOwner,
    VerifiedConfigSnapshot,
    Verifier,
    apply_manifest_delta,
    canonical_json_bytes,
    create_manifest_delta,
    test_kms_verifier,
    validate_config_manifest,
)

__all__ = [
    "ConfigDecision",
    "ConfigDistributionError",
    "ConfigSnapshotOwner",
    "Verifier",
    "VerifiedConfigSnapshot",
    "apply_manifest_delta",
    "canonical_json_bytes",
    "create_manifest_delta",
    "test_kms_verifier",
    "validate_config_manifest",
]
