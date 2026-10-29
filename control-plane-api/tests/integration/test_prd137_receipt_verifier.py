"""Cryptographic receipt contracts only; synthetic fixtures prove no attack outcomes."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.exceptions import InvalidSignature
from metahub_control.security_assurance.models import ScenarioExecution
from metahub_control.security_assurance.receipts import (
    MAX_RECEIPT_BYTES,
    MAX_TRUST_BYTES,
    FileAssuranceReceiptVerifier,
    TrustKey,
    execution_request_payload,
)
from metahub_control.security_assurance.service import (
    AssessmentExecutionRequest,
    RegisteredScenario,
)
from metahub_shared.config_distribution import canonical_json_bytes
from metahub_shared.runtime.config import load_app_config
from prd137_receipt_support import (
    TEST_KEY,
    b64,
    sign_receipt,
    synthetic_receipt,
    write_test_trust,
)

SCENARIOS = (
    ("api_idor", "public_contract"), ("write_authorization", "public_contract"),
    ("cache_owner", "cache"), ("queue_owner", "queue"), ("object_prefix", "object_store"),
    ("metrics_labels", "metrics"), ("dynamic_revocation", "authorization"),
    ("replay_fencing", "authorization"), ("late_response", "public_contract"),
    ("input_fuzz", "public_contract"),
)


@pytest.fixture
def signed(tmp_path):
    now = datetime.now(UTC)
    request = AssessmentExecutionRequest(
        security_assessment_id="sas_" + "a" * 32, workspace_id="wsp_" + "b" * 32,
        tenant_id="synthetic-test-owner", profile_code="incremental_release", profile_version=1,
        profile_sha256="1" * 64, profile_signature_sha256="2" * 64,
        generation=1, artifact_sha256="3" * 64, code_tree_sha256="4" * 64,
        migration_set_sha256="5" * 64, contract_set_sha256="6" * 64,
        lease_challenge_sha256="7" * 64, started_at=now,
        deadline_at=now + timedelta(minutes=30),
        scenarios=tuple(RegisteredScenario(
            scenario_code=code, target_kind=kind, owner_kind="workspace_resource",
            contract_code="test.contract", weakness_code="test_weakness", signature_sha256="8" * 64,
        ) for code, kind in SCENARIOS),
    )
    receipt = synthetic_receipt(request, tuple(ScenarioExecution(
        scenario_code=scenario.scenario_code, outcome="passed", duration_ms=0,
        evidence_sha256="9" * 64,
    ) for scenario in request.scenarios))
    path = tmp_path / "public-trust.json"
    bundle = write_test_trust(path)
    return request, receipt, path, bundle


def test_verified_signature_preserves_exact_receipts(signed):
    request, receipt, path, _ = signed
    assert FileAssuranceReceiptVerifier(path)(request, sign_receipt(receipt)) == receipt
    payload = execution_request_payload(request)
    assert request.workspace_id not in json.dumps(payload)
    assert request.tenant_id not in json.dumps(payload)
    assert len({run.seed_digest() for run in receipt.runs}) == 10
    assert all(run.seed_digest() != run.cleanup_digest() for run in receipt.runs)


def test_verified_archive_preserves_wire_and_public_trust(signed):
    request, receipt, path, bundle = signed
    content = (" \n" + json.dumps(json.loads(sign_receipt(receipt)), indent=2) + "\n").encode()
    verified = FileAssuranceReceiptVerifier(path).verify(request, content)
    assert verified.receipt == receipt
    assert verified.envelope_json.encode("utf-8") == content
    assert verified.request_json.encode() == canonical_json_bytes(
        execution_request_payload(request),
    )
    assert hashlib.sha256(verified.request_json.encode()).hexdigest() == receipt.request_sha256
    assert json.loads(verified.trust_key_json) == TrustKey.model_validate_json(
        json.dumps(bundle["keys"][0]),
    ).model_dump(mode="json")
    assert verified.verified_at >= receipt.finished_at
    assert verified.envelope_json not in repr(verified)


@pytest.mark.parametrize("encoding", ["utf-16", "utf-16-le", "utf-32"])
def test_receipt_transport_requires_utf8(signed, encoding):
    request, receipt, path, _ = signed
    with pytest.raises(ValueError):
        FileAssuranceReceiptVerifier(path).verify(
            request, sign_receipt(receipt).decode().encode(encoding),
        )


@pytest.mark.parametrize("field,value", [
    ("security_assessment_id", "sas_" + "f" * 32), ("workspace_id", "wsp_" + "f" * 32),
    ("tenant_id", "foreign-owner"), ("profile_code", "quarterly_full"), ("profile_version", 2),
    ("profile_sha256", "f" * 64), ("profile_signature_sha256", "f" * 64), ("generation", 2),
    ("artifact_sha256", "f" * 64), ("code_tree_sha256", "f" * 64),
    ("migration_set_sha256", "f" * 64), ("contract_set_sha256", "f" * 64),
    ("lease_challenge_sha256", "f" * 64),
])
def test_owner_build_profile_and_lease_replay_are_rejected(signed, field, value):
    request, receipt, path, _ = signed
    with pytest.raises(ValueError):
        FileAssuranceReceiptVerifier(path)(
            replace(request, **{field: value}), sign_receipt(receipt),
        )


@pytest.mark.parametrize("field", ["started_at", "deadline_at", "scenarios"])
def test_request_time_and_registry_are_bound(signed, field):
    request, receipt, path, _ = signed
    value = (tuple(reversed(request.scenarios)) if field == "scenarios"
             else getattr(request, field) + timedelta(seconds=1))
    with pytest.raises(ValueError):
        FileAssuranceReceiptVerifier(path)(
            replace(request, **{field: value}), sign_receipt(receipt),
        )


def test_tampered_outcome_is_not_authenticated(signed):
    request, receipt, path, _ = signed
    envelope = json.loads(sign_receipt(receipt))
    envelope["receipt"]["runs"][0]["result"]["evidence_sha256"] = "e" * 64
    with pytest.raises(InvalidSignature):
        FileAssuranceReceiptVerifier(path)(request, json.dumps(envelope).encode())


def test_another_signature_purpose_cannot_authorize_execution(signed):
    request, receipt, path, _ = signed
    envelope = json.loads(sign_receipt(receipt))
    envelope["signature"] = b64(TEST_KEY.sign(b"metahub.other-purpose.v1\0"))
    with pytest.raises(InvalidSignature):
        FileAssuranceReceiptVerifier(path)(request, json.dumps(envelope).encode())


@pytest.mark.parametrize("mode", [
    "expired", "future", "removed", "image", "policy", "duplicate", "oversized",
])
def test_deployment_trust_is_reloaded_and_pins_image_and_isolation(signed, mode):
    request, receipt, path, bundle = signed
    verifier = FileAssuranceReceiptVerifier(path)
    content = sign_receipt(receipt)
    assert verifier(request, content) == receipt
    key = bundle["keys"][0]
    now = datetime.now(UTC)
    if mode == "expired":
        key["not_after"] = (now - timedelta(seconds=1)).isoformat()
    elif mode == "future":
        key["not_before"] = (now + timedelta(seconds=1)).isoformat()
    elif mode == "removed":
        bundle["keys"] = []
    elif mode == "image":
        key["runner_image_sha256"] = "e" * 64
    elif mode == "policy":
        key["isolation_policy_sha256"] = "e" * 64
    elif mode == "duplicate":
        bundle["keys"].append(key.copy())
    path.write_text(
        " " * (MAX_TRUST_BYTES + 1) if mode == "oversized" else json.dumps(bundle),
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        verifier(request, content)


@pytest.mark.parametrize("mode", [
    "extra_field", "duplicate_json", "oversized", "empty", "bare_results", "bad_base64",
])
def test_unstructured_or_ambiguous_results_are_rejected(signed, mode):
    request, receipt, path, _ = signed
    value = json.loads(sign_receipt(receipt))
    if mode == "extra_field":
        value["target_url"] = "https://customer.invalid"
    elif mode == "empty":
        value["receipt"]["runs"] = []
    elif mode == "bare_results":
        value = [run.result.model_dump(mode="json") for run in receipt.runs]
    elif mode == "bad_base64":
        value["signature"] = "_" * 86
    content = json.dumps(value).encode()
    if mode == "duplicate_json":
        content = b'{"key_id":"first","key_id":"second"}'
    elif mode == "oversized":
        content = b" " * (MAX_RECEIPT_BYTES + 1)
    with pytest.raises((ValueError, InvalidSignature)):
        FileAssuranceReceiptVerifier(path)(request, content)


@pytest.mark.parametrize("mode", [
    "reordered", "duplicate_scenario", "duplicate_fixture", "missing_resource", "same_subject",
    "wrong_fixture", "future", "bad_duration", "cleanup_before_run", "zero_digest",
])
def test_even_signed_invalid_execution_facts_are_rejected(signed, mode):
    request, receipt, path, _ = signed
    value = receipt.model_dump(mode="json")
    run = value["runs"][0]
    if mode == "reordered":
        value["runs"].reverse()
    elif mode == "duplicate_scenario":
        value["runs"][1] = run
    elif mode == "duplicate_fixture":
        run["fixtures"].append(run["fixtures"][0])
    elif mode == "missing_resource":
        value["runs"][2]["fixtures"].pop()
    elif mode == "same_subject":
        run["target_subject_sha256"] = run["source_subject_sha256"]
    elif mode == "wrong_fixture":
        run["fixtures"][0]["fixture_id_sha256"] = "a" * 64
    elif mode == "future":
        value["finished_at"] = (datetime.now(UTC) + timedelta(minutes=1)).isoformat()
    elif mode == "bad_duration":
        run["result"]["duration_ms"] += 1
    elif mode == "cleanup_before_run":
        run["fixtures"][0]["cleanup_attempted_at"] = (
            request.started_at - timedelta(seconds=1)
        ).isoformat()
    else:
        run["target_subject_sha256"] = "0" * 64
    with pytest.raises(ValueError):
        changed = type(receipt).model_validate_json(json.dumps(value))
        FileAssuranceReceiptVerifier(path)(request, sign_receipt(changed))


def test_public_trust_config_is_private_and_loaded(monkeypatch, tmp_path):
    path = str(tmp_path / "public-trust.json")
    monkeypatch.setenv("METAHUB_ASSURANCE_RECEIPT_TRUST_BUNDLE_PATH", path)
    config = load_app_config("control-plane-api")
    assert config.assurance_receipt_trust_bundle_path == path
    assert "assurance_receipt_trust_bundle_path" not in config.model_dump()
    assert path not in repr(config)
