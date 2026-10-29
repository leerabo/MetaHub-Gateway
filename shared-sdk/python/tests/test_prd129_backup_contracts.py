from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from metahub_shared.private_gateway import (
    BACKUP_DOMAIN_CODES,
    EdgeBackupContractError,
    EdgeBackupPackage,
    build_edge_backup,
    parse_signed_backup,
    verify_edge_backup,
)
from metahub_shared.private_gateway.desired_state import canonical_json_bytes
from metahub_shared.runtime.errors import ERROR_SPECS

NOW = datetime(2026, 8, 28, 10, 0, tzinfo=UTC)
WORKSPACE_ID = "wsp_" + "1" * 32
TENANT_ID = "tenant_prd129"
DEPLOYMENT_ID = "edp_" + "2" * 32
BACKUP_ID = "ebk_" + "3" * 32


def test_backup_error_catalog_is_fixed_and_retryability_is_explicit() -> None:
    expected = {
        "edge_backup.request_invalid": 400,
        "edge_backup.not_found": 404,
        "edge_backup.drill_not_found": 404,
        "edge_backup.idempotency_conflict": 409,
        "edge_backup.generation_conflict": 409,
        "edge_backup.report_conflict": 409,
        "edge_backup.source_ineligible": 409,
        "edge_backup.drill_active": 409,
        "edge_backup.approval_conflict": 409,
        "edge_backup.drill_report_conflict": 409,
        "edge_backup.state_conflict": 409,
        "edge_backup.unavailable": 503,
    }
    for code, status in expected.items():
        assert ERROR_SPECS[code].http_status == status
    assert ERROR_SPECS["edge_backup.unavailable"].retryable is True
    assert all(
        ERROR_SPECS[code].retryable is False
        for code in expected
        if code != "edge_backup.unavailable"
    )


def _domains() -> dict[str, bytes]:
    return {
        domain: (domain.encode("ascii") + b"\x00") * 150
        for domain in BACKUP_DOMAIN_CODES
    }


def _wrap(data_key: bytes) -> bytes:
    return b"customer-kms-envelope-v1\x00" + data_key


def _unwrap(envelope: bytes) -> bytes:
    prefix = b"customer-kms-envelope-v1\x00"
    if not envelope.startswith(prefix):
        raise ValueError
    return envelope[len(prefix) :]


def _package(*, schema_head: str = "0121") -> EdgeBackupPackage:
    return build_edge_backup(
        backup_id=BACKUP_ID,
        workspace_id=WORKSPACE_ID,
        tenant_id=TENANT_ID,
        source_deployment_id=DEPLOYMENT_ID,
        source_generation=17,
        source_schema_head=schema_head,
        domains=_domains(),
        key_id="backup-signing-v4",
        kms_key_id="projects/customer/keys/gateway-backup",
        key_version=4,
        wrap_data_key=_wrap,
        signing_key=Ed25519PrivateKey.generate(),
        created_at=NOW,
        expires_at=NOW + timedelta(days=30),
        chunk_bytes=1024,
    )


def _verify(package: EdgeBackupPackage, **overrides: object):
    values: dict[str, object] = {
        "expected_workspace_id": WORKSPACE_ID,
        "expected_tenant_id": TENANT_ID,
        "expected_source_deployment_id": DEPLOYMENT_ID,
        "target_schema_head": "0121",
        "unwrap_data_key": _unwrap,
        "now": NOW + timedelta(minutes=1),
    }
    values.update(overrides)
    return verify_edge_backup(package, **values)  # type: ignore[arg-type]


def _error(package: EdgeBackupPackage, code: str, **overrides: object) -> None:
    with pytest.raises(EdgeBackupContractError) as caught:
        _verify(package, **overrides)
    assert caught.value.code == code
    assert str(caught.value) == code


def test_backup_encrypts_chunks_signs_manifest_and_restores_all_domains() -> None:
    package = _package()

    assert len(package.signed.manifest.chunks) > len(BACKUP_DOMAIN_CODES)
    assert set(package.chunks) == {
        descriptor.chunk_id for descriptor in package.signed.manifest.chunks
    }
    assert all(chunk not in _domains().values() for chunk in package.chunks.values())
    assert "customer-kms-envelope" not in repr(package)

    verified = _verify(package)
    assert verified.domains == _domains()
    serialized = canonical_json_bytes(package.signed.model_dump(mode="json"))
    assert parse_signed_backup(serialized) == package.signed


@pytest.mark.parametrize("target_schema", ["0121", "0122"])
def test_restore_accepts_only_current_or_previous_backup_schema(
    target_schema: str,
) -> None:
    assert _verify(_package(), target_schema_head=target_schema).domains == _domains()


def test_restore_rejects_wrong_owner_schema_time_and_key_without_plaintext() -> None:
    package = _package()

    _error(package, "edge_backup.owner_mismatch", expected_workspace_id="wsp_" + "9" * 32)
    _error(package, "edge_backup.owner_mismatch", expected_tenant_id="tenant_other")
    _error(
        package,
        "edge_backup.owner_mismatch",
        expected_source_deployment_id="edp_" + "9" * 32,
    )
    _error(package, "edge_backup.schema_incompatible", target_schema_head="0120")
    _error(package, "edge_backup.expired", now=NOW + timedelta(days=31))
    _error(package, "edge_backup.integrity_failed", unwrap_data_key=lambda _: b"x" * 32)


def test_restore_rejects_missing_or_corrupt_chunk_before_returning_domains() -> None:
    package = _package()
    first_id = package.signed.manifest.chunks[0].chunk_id
    missing = dict(package.chunks)
    del missing[first_id]
    _error(
        EdgeBackupPackage(package.signed, package.key_envelope, missing),
        "edge_backup.chunk_missing",
    )

    corrupt = dict(package.chunks)
    corrupt[first_id] = bytes([corrupt[first_id][0] ^ 1]) + corrupt[first_id][1:]
    _error(
        EdgeBackupPackage(package.signed, package.key_envelope, corrupt),
        "edge_backup.chunk_invalid",
    )


def test_restore_rejects_manifest_and_envelope_tampering() -> None:
    package = _package()
    bad_signed = package.signed.model_copy(update={"manifest_sha256": "0" * 64})
    _error(
        EdgeBackupPackage(bad_signed, package.key_envelope, dict(package.chunks)),
        "edge_backup.manifest_invalid",
    )
    bad_envelope = package.key_envelope.model_copy(update={"key_version": 5})
    _error(
        EdgeBackupPackage(package.signed, bad_envelope, dict(package.chunks)),
        "edge_backup.envelope_invalid",
    )
