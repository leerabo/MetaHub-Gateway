from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from metahub_shared.private_gateway import (
    DIAGNOSTIC_COMMAND_SCHEMA_VERSION,
    DIAGNOSTIC_RESULT_SCHEMA_VERSION,
    DIAGNOSTIC_SIGNATURE_PURPOSE,
    DiagnosticContractError,
    DiagnosticExecutionReport,
    DiagnosticGrantContract,
    DiagnosticUnsignedCommand,
    RuntimeConnectivityInvocation,
    RuntimeConnectivityResult,
    RuntimeVersionInvocation,
    RuntimeVersionResult,
    RuntimeWatermarksInvocation,
    diagnostic_grant_sha256,
    diagnostic_invocation_sha256,
    diagnostic_result_sha256,
    reject_forbidden_diagnostic_fields,
    sign_diagnostic_command,
    verify_diagnostic_command,
)
from pydantic import ValidationError

DEPLOYMENT_ID = "edp_" + "1" * 32
COMMAND_ID = "dcm_" + "2" * 32
GRANT_ID = "dgr_" + "3" * 32


def _time(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.from_private_bytes(bytes(range(32)))


def _public_key(private_key: Ed25519PrivateKey) -> bytes:
    return private_key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )


def _unsigned(now: datetime) -> DiagnosticUnsignedCommand:
    invocation = RuntimeWatermarksInvocation(
        command_code="runtime.watermarks",
        watermark_codes=(
            "desired_generation",
            "applied_generation",
            "lkg_generation",
        ),
    )
    return DiagnosticUnsignedCommand(
        schema_version=DIAGNOSTIC_COMMAND_SCHEMA_VERSION,
        signature_purpose=DIAGNOSTIC_SIGNATURE_PURPOSE,
        command_id=COMMAND_ID,
        grant_id=GRANT_ID,
        deployment_id=DEPLOYMENT_ID,
        generation=4,
        revocation_generation=7,
        grant_sha256="4" * 64,
        invocation=invocation,
        parameters_sha256=diagnostic_invocation_sha256(invocation),
        issued_at=_time(now),
        not_before=_time(now),
        deadline_at=_time(now + timedelta(seconds=30)),
        expires_at=_time(now + timedelta(minutes=2)),
        maximum_result_bytes=16_384,
        algorithm="Ed25519",
        kid="edge-ds-key-v1",
    )


def test_signed_diagnostic_command_round_trip_and_authority_fail_closed() -> None:
    now = datetime(2026, 8, 27, 12, 0, tzinfo=UTC)
    private_key = _key()
    envelope = sign_diagnostic_command(_unsigned(now), private_key)
    verified = verify_diagnostic_command(
        envelope.model_dump(mode="json"),
        trusted_keys={"edge-ds-key-v1": _public_key(private_key)},
        expected_deployment_id=DEPLOYMENT_ID,
        minimum_generation=4,
        revocation_generation=7,
        now=now + timedelta(seconds=1),
    )
    assert verified.command.command_id == COMMAND_ID
    assert len(verified.canonical_envelope) > 512
    assert len(verified.envelope_sha256) == 64

    for changes, code in (
        ({"expected_deployment_id": "edp_" + "9" * 32}, "edge_diagnostic.command_untrusted"),
        ({"minimum_generation": 5}, "edge_diagnostic.command_untrusted"),
        ({"revocation_generation": 8}, "edge_diagnostic.command_untrusted"),
        (
            {"now": now + timedelta(minutes=3)},
            "edge_diagnostic.command_expired",
        ),
    ):
        arguments = {
            "trusted_keys": {"edge-ds-key-v1": _public_key(private_key)},
            "expected_deployment_id": DEPLOYMENT_ID,
            "minimum_generation": 4,
            "revocation_generation": 7,
            "now": now,
            **changes,
        }
        with pytest.raises(DiagnosticContractError, match=code):
            verify_diagnostic_command(envelope.model_dump(mode="json"), **arguments)

    tampered = envelope.model_dump(mode="json")
    tampered["generation"] = 5
    with pytest.raises(DiagnosticContractError, match="edge_diagnostic.command_untrusted"):
        verify_diagnostic_command(
            tampered,
            trusted_keys={"edge-ds-key-v1": _public_key(private_key)},
            expected_deployment_id=DEPLOYMENT_ID,
            minimum_generation=4,
            revocation_generation=7,
            now=now,
        )


def test_invocation_and_grant_are_exact_and_hash_bound() -> None:
    now = datetime(2026, 8, 27, 12, 0, tzinfo=UTC)
    invocation = RuntimeConnectivityInvocation(
        command_code="runtime.connectivity_summary",
        target_code="control_plane",
    )
    grant = DiagnosticGrantContract(
        grant_id=GRANT_ID,
        deployment_id=DEPLOYMENT_ID,
        invocation=invocation,
        parameters_sha256=diagnostic_invocation_sha256(invocation),
        max_runs=1,
        deployment_revision=3,
        revocation_generation=7,
        workspace_authority_revision=4,
        platform_authority_revision=5,
        approved_at=_time(now),
        expires_at=_time(now + timedelta(minutes=2)),
    )
    assert len(diagnostic_grant_sha256(grant)) == 64

    invalid = grant.model_dump(mode="json")
    invalid["parameters_sha256"] = "0" * 64
    with pytest.raises(ValidationError):
        DiagnosticGrantContract.model_validate(invalid)

    with pytest.raises(ValidationError):
        RuntimeVersionInvocation.model_validate(
            {"command_code": "runtime.version", "arguments": ["--all"]}
        )


def test_result_schema_and_report_reject_free_text_or_digest_drift() -> None:
    started = datetime(2026, 8, 27, 12, 0, tzinfo=UTC)
    result = RuntimeVersionResult(
        command_code="runtime.version",
        agent_version="1.2.3",
        gateway_schema_version=118,
        os_family="linux",
        architecture="x86_64",
    )
    report = DiagnosticExecutionReport(
        schema_version=DIAGNOSTIC_RESULT_SCHEMA_VERSION,
        command_id=COMMAND_ID,
        grant_id=GRANT_ID,
        deployment_id=DEPLOYMENT_ID,
        generation=4,
        status="completed",
        reason_code="completed",
        result=result,
        result_sha256=diagnostic_result_sha256(result),
        started_at=_time(started),
        completed_at=_time(started + timedelta(milliseconds=125)),
        duration_ms=125,
    )
    assert report.result_sha256 == diagnostic_result_sha256(result)

    drifted = report.model_dump(mode="json")
    drifted["result_sha256"] = "0" * 64
    with pytest.raises(ValidationError):
        DiagnosticExecutionReport.model_validate(drifted)

    with pytest.raises(DiagnosticContractError, match="edge_diagnostic.forbidden_field"):
        reject_forbidden_diagnostic_fields(
            {"command_code": "runtime.version", "stdout": "private material"}
        )

    with pytest.raises(ValidationError):
        RuntimeConnectivityResult(
            command_code="runtime.connectivity_summary",
            target_code="provider_pool",
            status_code="reachable",
            latency_bucket="normal",
            checked_target_count=2,
            reachable_target_count=1,
        )
