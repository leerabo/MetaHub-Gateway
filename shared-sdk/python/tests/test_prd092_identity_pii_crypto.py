"""PRD-092 owner-bound encryption contracts for unverified registration email."""

from __future__ import annotations

import pytest
from metahub_shared.identity.crypto import (
    IdentityPiiCryptoError,
    IdentityPiiOwner,
    decrypt_identity_pii,
    encrypt_identity_pii,
)

SECRET = "prd092-identity-pii-encryption-secret-0123456789abcdef"
EMAIL = "private.registration@example.com"
INTENT_ID = "rgi_" + "1" * 32


def _owner(intent_id: str = INTENT_ID) -> IdentityPiiOwner:
    return IdentityPiiOwner(
        purpose="registration",
        intent_id=intent_id,
        field_kind="email_normalized",
        key_version=1,
    )


def test_identity_pii_round_trip_is_owner_bound_and_contains_no_plaintext() -> None:
    ciphertext = encrypt_identity_pii(EMAIL, secret=SECRET, owner=_owner())

    assert EMAIL not in ciphertext
    assert ciphertext.startswith("metahub-identity-pii-aesgcm-v1:1:email_normalized:")
    assert decrypt_identity_pii(ciphertext, secret=SECRET, owner=_owner()) == EMAIL

    with pytest.raises(IdentityPiiCryptoError, match="^identity.unavailable$"):
        decrypt_identity_pii(
            ciphertext,
            secret=SECRET,
            owner=_owner("rgi_" + "2" * 32),
        )


@pytest.mark.parametrize("mutation", ["tag", "secret", "format"])
def test_identity_pii_corruption_is_a_fixed_unavailable_failure(mutation: str) -> None:
    ciphertext = encrypt_identity_pii(EMAIL, secret=SECRET, owner=_owner())
    secret = SECRET
    if mutation == "tag":
        ciphertext = ciphertext[:-1] + ("A" if ciphertext[-1] != "A" else "B")
    elif mutation == "secret":
        secret = "another-prd092-pii-encryption-secret-0123456789abcdef"
    else:
        ciphertext = ciphertext.replace("aesgcm-v1", "aesgcm-v2", 1)

    with pytest.raises(IdentityPiiCryptoError, match="^identity.unavailable$"):
        decrypt_identity_pii(ciphertext, secret=secret, owner=_owner())
