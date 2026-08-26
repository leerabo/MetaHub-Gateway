from __future__ import annotations

from dataclasses import replace

import pytest
from metahub_shared.private_gateway import (
    EncryptedLocalSecret,
    LocalKek,
    LocalKekKeyring,
    LocalSecretError,
    LocalSecretOwner,
    SecretReference,
    decrypt_local_secret,
    encrypt_local_secret,
    rewrap_local_secret,
    secret_reference_sha256,
)
from pydantic import ValidationError

DEPLOYMENT_ID = "edp_" + "1" * 32
REFERENCE_ID = "lsr_" + "2" * 32
BINDING_DIGEST = "3" * 64


def _owner(**changes: object) -> LocalSecretOwner:
    values: dict[str, object] = {
        "deployment_id": DEPLOYMENT_ID,
        "tenant_id": "ten_private_001",
        "workspace_id": "wsp_private_001",
        "reference_id": REFERENCE_ID,
        "version": 1,
        "kind": "api_key",
        "binding_digest": BINDING_DIGEST,
    }
    values.update(changes)
    return LocalSecretOwner(**values)  # type: ignore[arg-type]


def _reference() -> SecretReference:
    return SecretReference.model_validate(
        {
            "schema_version": "1",
            "reference_id": REFERENCE_ID,
            "deployment_id": DEPLOYMENT_ID,
            "tenant_id": "ten_private_001",
            "workspace_id": "wsp_private_001",
            "provider_connection_id": "pcn_private_001",
            "label": "Primary provider credential",
            "kind": "api_key",
            "status": "active",
            "revision": 2,
            "active_version": 1,
            "last_test_outcome": "succeeded",
            "capabilities": ["chat_completions", "models"],
            "rotated_at": "2026-08-27T02:00:00.000Z",
            "updated_at": "2026-08-27T02:00:00.000Z",
        }
    )


def test_cloud_reference_is_exact_safe_projection() -> None:
    reference = _reference()
    first = secret_reference_sha256(reference)
    assert secret_reference_sha256(reference) == first
    assert set(reference.model_dump()) == {
        "schema_version",
        "reference_id",
        "deployment_id",
        "tenant_id",
        "workspace_id",
        "provider_connection_id",
        "label",
        "kind",
        "status",
        "revision",
        "active_version",
        "last_test_outcome",
        "capabilities",
        "rotated_at",
        "updated_at",
    }

    invalid = reference.model_dump(mode="json")
    invalid["value"] = "forbidden"
    with pytest.raises(ValidationError):
        SecretReference.model_validate(invalid)

    invalid = reference.model_dump(mode="json")
    invalid["capabilities"] = ["models", "chat_completions"]
    with pytest.raises(ValidationError, match="reference_invalid"):
        SecretReference.model_validate(invalid)

    real_owner = reference.model_dump(mode="json")
    real_owner["tenant_id"] = "944f3e6d-8b66-584e-b095-7cac890a5f87"
    assert SecretReference.model_validate(real_owner).tenant_id.startswith("9")

    invalid = reference.model_dump(mode="json")
    invalid["status"] = "disabled"
    with pytest.raises(ValidationError, match="reference_invalid"):
        SecretReference.model_validate(invalid)


def test_envelope_binds_every_owner_dimension_and_lease_is_zeroed() -> None:
    keyring = LocalKekKeyring(LocalKek(2, bytes(range(32))))
    plaintext = bytearray(index % 251 for index in range(257))
    encrypted = encrypt_local_secret(plaintext, owner=_owner(), keyring=keyring)
    assert isinstance(encrypted, EncryptedLocalSecret)
    assert encrypted.kek_version == 2
    assert "ciphertext=" not in repr(encrypted)
    assert "wrapped_data_key=" not in repr(encrypted)

    lease = decrypt_local_secret(encrypted, owner=_owner(), keyring=keyring)
    view = lease.view()
    assert bytes(view) == bytes(plaintext)
    assert "redacted" in repr(lease)
    lease.close()
    assert bytes(view) == b"\x00" * len(plaintext)
    with pytest.raises(LocalSecretError, match="lease_closed"):
        lease.view()

    for changed in (
        {"deployment_id": "edp_" + "4" * 32},
        {"tenant_id": "ten_private_002"},
        {"workspace_id": "wsp_private_002"},
        {"reference_id": "lsr_" + "5" * 32},
        {"version": 2},
        {"kind": "bearer_token"},
        {"binding_digest": "6" * 64},
    ):
        with pytest.raises(LocalSecretError, match="integrity_failed"):
            decrypt_local_secret(encrypted, owner=_owner(**changed), keyring=keyring)


def test_kek_rotation_rewraps_dek_and_previous_key_can_drain() -> None:
    previous = LocalKek(1, bytes(range(32)))
    current = LocalKek(2, bytes(reversed(range(32))))
    old_ring = LocalKekKeyring(previous)
    transition_ring = LocalKekKeyring(current=current, previous=previous)
    encrypted = encrypt_local_secret(b"x" * 37, owner=_owner(), keyring=old_ring)

    with decrypt_local_secret(encrypted, owner=_owner(), keyring=transition_ring) as lease:
        assert bytes(lease.view()) == b"x" * 37
    rewrapped = rewrap_local_secret(encrypted, owner=_owner(), keyring=transition_ring)
    assert rewrapped.kek_version == 2
    assert rewrapped.ciphertext == encrypted.ciphertext
    assert rewrapped.value_nonce == encrypted.value_nonce
    with decrypt_local_secret(
        rewrapped, owner=_owner(), keyring=LocalKekKeyring(current)
    ) as lease:
        assert bytes(lease.view()) == b"x" * 37

    with pytest.raises(LocalSecretError, match="key_unavailable"):
        decrypt_local_secret(encrypted, owner=_owner(), keyring=LocalKekKeyring(current))
    with pytest.raises(LocalSecretError, match="keyring_invalid"):
        LocalKekKeyring(current=current, previous=replace(current, version=1))


def test_randomness_and_envelope_shape_fail_closed() -> None:
    keyring = LocalKekKeyring(LocalKek(1, bytes(range(32))))

    def repeated(length: int) -> bytes:
        return b"r" * length

    with pytest.raises(LocalSecretError, match="configuration_unavailable"):
        encrypt_local_secret(b"x", owner=_owner(), keyring=keyring, random_bytes=repeated)

    encrypted = encrypt_local_secret(b"x", owner=_owner(), keyring=keyring)
    malformed = replace(encrypted, ciphertext=encrypted.ciphertext[:-1])
    with pytest.raises(LocalSecretError, match="integrity_failed"):
        decrypt_local_secret(malformed, owner=_owner(), keyring=keyring)
