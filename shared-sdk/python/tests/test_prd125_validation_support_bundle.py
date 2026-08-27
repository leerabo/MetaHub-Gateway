"""PRD-125 delivery-validation and redacted support-bundle contract tests."""

from __future__ import annotations

import hashlib
import hmac
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from metahub_shared.private_gateway import (
    SUPPORT_BUNDLE_COLLECTOR_CODES,
    DeploymentValidationReport,
    SupportBundleBuilder,
    SupportBundleUploadMetadata,
    ValidationCheckEvidence,
    ValidationContractError,
    canonical_edge_json_bytes,
    decrypt_support_bundle,
    encrypt_support_bundle,
    support_bundle_approval_proof_sha256,
    support_bundle_manifest_sha256,
    validation_report_sha256,
)
from pydantic import ValidationError

NOW = datetime(2026, 8, 27, 5, 0, tzinfo=UTC)
BUNDLE_ID = "esb_" + "1" * 32
DEPLOYMENT_ID = "edp_" + "2" * 32
VALIDATION_ID = "dvr_" + "3" * 32
SCANNER_KEY = bytes(range(32))
ENCRYPTION_SECRET = b"prd-125-test-encryption-secret-material"


def _collector_values() -> dict[str, object]:
    return {
        "runtime.version": {"agent_version": "1.0.0", "schema": 117},
        "runtime.watermarks": {"configuration_generation": 7, "usage_cursor": 9},
        "runtime.health": {"status": "ready", "checks_passed": 4},
        "runtime.errors": {"codes": ["provider.timeout"], "count": 1},
        "runtime.logs": '{"code":"gateway.ready","count":1}\n',
    }


def _builder(
    root: Path,
    *,
    values: dict[str, object] | None = None,
    known_secret_hmacs: tuple[str, ...] = (),
) -> SupportBundleBuilder:
    selected = values or _collector_values()
    return SupportBundleBuilder(
        root,
        {code: (lambda value=selected[code]: value) for code in selected},
        scanner_hmac_key=SCANNER_KEY,
        known_secret_hmacs=known_secret_hmacs,
        now=lambda: NOW,
    )


def _prepared(root: Path):
    return _builder(root).build(
        bundle_id=BUNDLE_ID,
        deployment_id=DEPLOYMENT_ID,
        collector_codes=SUPPORT_BUNDLE_COLLECTOR_CODES,
        expires_at=NOW + timedelta(hours=1),
    )


def test_validation_report_is_ordered_immutable_and_hash_stable() -> None:
    first = ValidationCheckEvidence(
        code="device_certificate",
        outcome="passed",
        reason_code="certificate.valid",
        evidence_sha256="a" * 64,
        duration_ms=100,
    )
    second = ValidationCheckEvidence(
        code="signed_config_lkg",
        outcome="failed",
        reason_code="configuration.missing",
        recovery_action_code="configuration.republish",
        evidence_sha256="b" * 64,
        duration_ms=200,
    )
    report = DeploymentValidationReport(
        schema_version="metahub.delivery-validation/v1",
        validation_id=VALIDATION_ID,
        deployment_id=DEPLOYMENT_ID,
        plan_version="edge-delivery-v1",
        attempt=1,
        input_contract_sha256="c" * 64,
        outcome="failed",
        checks=(first, second),
        started_at="2026-08-27T05:00:00.000Z",
        completed_at="2026-08-27T05:00:00.200Z",
        duration_ms=200,
    )
    assert validation_report_sha256(report) == validation_report_sha256(
        DeploymentValidationReport.model_validate_json(report.model_dump_json())
    )
    with pytest.raises(ValidationError, match="frozen_instance"):
        report.attempt = 2
    with pytest.raises(ValidationError, match="edge_validation.report_invalid"):
        DeploymentValidationReport(
            **{
                **report.model_dump(),
                "checks": (second, first),
            }
        )


def test_support_bundle_round_trip_uses_fixed_manifest_and_authenticated_ciphertext(
    tmp_path: Path,
) -> None:
    prepared = _prepared(tmp_path / "bundle")
    assert tuple(entry.collector_code for entry in prepared.manifest.entries) == (
        SUPPORT_BUNDLE_COLLECTOR_CODES
    )
    assert prepared.archive_path.name == f"{BUNDLE_ID}.zip"

    encrypted = encrypt_support_bundle(
        prepared,
        tmp_path / "cloud" / "bundle.enc",
        encryption_secret=ENCRYPTION_SECRET,
    )
    restored = decrypt_support_bundle(
        encrypted.object_path,
        tmp_path / "restored.zip",
        manifest=prepared.manifest,
        encryption_secret=ENCRYPTION_SECRET,
        expected_object_sha256=encrypted.object_sha256,
    )
    assert restored.read_bytes() == prepared.archive_path.read_bytes()

    tampered = bytearray(encrypted.object_path.read_bytes())
    tampered[-17] ^= 1
    encrypted.object_path.write_bytes(tampered)
    with pytest.raises(ValidationContractError, match="support_bundle.integrity_failed"):
        decrypt_support_bundle(
            encrypted.object_path,
            tmp_path / "tampered.zip",
            manifest=prepared.manifest,
            encryption_secret=ENCRYPTION_SECRET,
            expected_object_sha256=hashlib.sha256(tampered).hexdigest(),
        )
    assert not (tmp_path / "tampered.zip").exists()


def test_support_bundle_approval_proof_binds_owner_manifest_time_and_revision(
    tmp_path: Path,
) -> None:
    manifest = _prepared(tmp_path / "proof").manifest
    values = {
        "bundle_id": manifest.bundle_id,
        "deployment_id": manifest.deployment_id,
        "tenant_id": "944f3e6d-8b66-584e-b095-7cac890a5f87",
        "workspace_id": "wsp_private_001",
        "manifest_sha256": hashlib.sha256(b"manifest").hexdigest(),
        "approved_by_account_id": "acc_security_001",
        "approved_at": "2026-08-27T05:01:00.000Z",
        "local_revision": 2,
    }
    proof = support_bundle_approval_proof_sha256(**values)
    assert len(proof) == 64
    assert proof != support_bundle_approval_proof_sha256(
        **{**values, "approved_by_account_id": "acc_security_002"}
    )
    with pytest.raises(ValidationContractError, match="support_bundle.approval_invalid"):
        support_bundle_approval_proof_sha256(**{**values, "local_revision": 1})

    upload_values = {
        **values,
        "manifest_sha256": support_bundle_manifest_sha256(manifest),
    }
    metadata = SupportBundleUploadMetadata(
        schema_version="metahub.support-bundle-upload/v1",
        manifest=manifest,
        manifest_sha256=upload_values["manifest_sha256"],
        object_sha256="c" * 64,
        object_size_bytes=manifest.archive_size_bytes + 33,
        approved_by_account_id=values["approved_by_account_id"],
        approval_proof_sha256=support_bundle_approval_proof_sha256(**upload_values),
        approved_at=values["approved_at"],
    )
    encoded = canonical_edge_json_bytes(metadata.model_dump(mode="json"))
    restored = SupportBundleUploadMetadata.model_validate_json(encoded)
    assert restored == metadata


@pytest.mark.parametrize(
    ("collector", "value"),
    [
        ("runtime.health", {"api_key": "not-allowed-in-diagnostics"}),
        ("runtime.logs", '{"secret":"not-allowed-in-diagnostics"}\n'),
        ("runtime.logs", "not-json\n"),
        (
            "runtime.errors",
            {"codes": ["sk_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"]},
        ),
    ],
)
def test_support_bundle_rejects_sensitive_or_unstructured_content(
    tmp_path: Path, collector: str, value: object
) -> None:
    values = _collector_values()
    values[collector] = value
    with pytest.raises(ValidationContractError):
        _builder(tmp_path / collector.replace(".", "-"), values=values).build(
            bundle_id=BUNDLE_ID,
            deployment_id=DEPLOYMENT_ID,
            collector_codes=(collector,),
            expires_at=NOW + timedelta(hours=1),
        )


def test_support_bundle_rejects_known_local_value_and_naive_expiry(tmp_path: Path) -> None:
    known = "customer-known-value-0000"
    digest = hmac.new(SCANNER_KEY, known.encode("utf-8"), hashlib.sha256).hexdigest()
    values = _collector_values()
    values["runtime.health"] = {"status": known}
    with pytest.raises(ValidationContractError, match="support_bundle.sensitive_detected"):
        _builder(
            tmp_path / "known",
            values=values,
            known_secret_hmacs=(digest,),
        ).build(
            bundle_id=BUNDLE_ID,
            deployment_id=DEPLOYMENT_ID,
            collector_codes=("runtime.health",),
            expires_at=NOW + timedelta(hours=1),
        )

    with pytest.raises(ValidationContractError, match="support_bundle.request_invalid"):
        _builder(tmp_path / "naive").build(
            bundle_id=BUNDLE_ID,
            deployment_id=DEPLOYMENT_ID,
            collector_codes=("runtime.health",),
            expires_at=datetime(2026, 8, 27, 6, 0),
        )


def test_support_bundle_generation_fails_closed_at_five_minute_deadline(
    tmp_path: Path,
) -> None:
    ticks = iter((0.0, 0.0, 300.0))
    builder = SupportBundleBuilder(
        tmp_path,
        {
            code: (lambda value=value: value)
            for code, value in _collector_values().items()
        },
        scanner_hmac_key=b"s" * 32,
        now=lambda: NOW,
        monotonic=lambda: next(ticks),
    )
    with pytest.raises(ValidationContractError) as timeout:
        builder.build(
            bundle_id=BUNDLE_ID,
            deployment_id=DEPLOYMENT_ID,
            collector_codes=SUPPORT_BUNDLE_COLLECTOR_CODES,
            expires_at=NOW + timedelta(minutes=10),
        )
    assert timeout.value.code == "support_bundle.generation_timeout"
    assert list(tmp_path.iterdir()) == []
