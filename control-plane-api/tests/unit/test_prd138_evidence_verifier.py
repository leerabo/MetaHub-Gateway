"""Real signature contracts, not evidence of real QA outcomes or elapsed hours."""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from metahub_control.launch_governance.service import (
    EvidenceVerificationRequest,
    evidence_envelope_sha256,
)
from metahub_control.launch_governance.verifier import (
    MAX_TRUST_BYTES,
    FileEvidenceVerifier,
    evidence_signing_bytes,
    install_launch_evidence_verifier,
)
from metahub_shared.runtime.config import load_app_config

SETTING = "METAHUB_LAUNCH_EVIDENCE_TRUST_BUNDLE_PATH"
ROOT = Path(__file__).resolve().parents[3]


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _request() -> EvidenceVerificationRequest:
    now = datetime.now(UTC)
    request = EvidenceVerificationRequest(
        launch_candidate_id="lvc_" + "1" * 32, candidate_sha256="1" * 64,
        launch_gate_id="lgt_" + "2" * 32, gate_code="api", policy_sha256="2" * 64,
        workspace_id="wsp_" + "3" * 32, tenant_id="tenant_verifier", artifact_sha256="3" * 64,
        verifier_code="api-verifier", verifier_version=1, outcome="passed",
        metric_value=1.0, metric_unit="pass_ratio", environment_sha256="4" * 64,
        provenance_sha256="5" * 64, started_at=now - timedelta(minutes=1),
        finished_at=now, envelope_sha256="",
    )
    return replace(request, envelope_sha256=evidence_envelope_sha256(request))


def _bundle(private: Ed25519PrivateKey):
    now = datetime.now(UTC)
    return {"schema_version": "metahub.launch-evidence-trust.v1", "keys": [{
        "key_id": "api-current", "gate_code": "api", "verifier_code": "api-verifier",
        "verifier_version": 1, "public_key": _b64(private.public_key().public_bytes_raw()),
        "not_before": (now - timedelta(hours=1)).isoformat(),
        "not_after": (now + timedelta(hours=1)).isoformat(),
    }]}


@pytest.fixture
def signed(tmp_path):
    private = Ed25519PrivateKey.generate()
    request = _request()
    signature = _b64(private.sign(evidence_signing_bytes(request)))
    path = tmp_path / "public-trust.json"
    bundle = _bundle(private)
    path.write_text(json.dumps(bundle), encoding="utf-8")
    return private, request, signature, path, bundle


def test_real_signature_and_canonical_utc_time(signed):
    _, request, signature, path, _ = signed
    verifier = FileEvidenceVerifier(path)
    assert verifier(request, signature) is True
    equivalent = replace(
        request, started_at=request.started_at.astimezone(timezone(timedelta(hours=8)))
    )
    assert verifier(equivalent, signature) is True


@pytest.mark.parametrize("field,value", [
    ("launch_candidate_id", "lvc_" + "9" * 32), ("candidate_sha256", "9" * 64),
    ("launch_gate_id", "lgt_" + "9" * 32), ("gate_code", "bb"),
    ("policy_sha256", "9" * 64), ("workspace_id", "wsp_" + "9" * 32),
    ("tenant_id", "other_tenant"), ("artifact_sha256", "9" * 64),
    ("verifier_code", "other-verifier"), ("verifier_version", 2),
    ("outcome", "failed"), ("metric_value", 0.0), ("metric_unit", "seconds"),
    ("environment_sha256", "9" * 64), ("provenance_sha256", "9" * 64),
])
def test_every_signed_owner_policy_and_result_field_is_bound(signed, field, value):
    _, request, signature, path, _ = signed
    changed = replace(request, **{field: value})
    verifier = FileEvidenceVerifier(path)
    with pytest.raises(ValueError, match="digest mismatch"):
        verifier(changed, signature)
    changed = replace(changed, envelope_sha256=evidence_envelope_sha256(changed))
    assert verifier(changed, signature) is False


@pytest.mark.parametrize("field", ["started_at", "finished_at"])
def test_evidence_time_is_signed(signed, field):
    _, request, signature, path, _ = signed
    changed = replace(request, **{field: getattr(request, field) - timedelta(seconds=1)})
    changed = replace(changed, envelope_sha256=evidence_envelope_sha256(changed))
    assert FileEvidenceVerifier(path)(changed, signature) is False


def test_other_signature_purpose_is_rejected(signed):
    private, request, _, path, _ = signed
    signature = _b64(private.sign(b"metahub.other.v1\0" + bytes.fromhex(request.envelope_sha256)))
    assert FileEvidenceVerifier(path)(request, signature) is False


@pytest.mark.parametrize("mode", ["expired", "future", "removed", "wrong_key", "wrong_gate"])
def test_trust_changes_apply_to_the_next_submission(signed, mode):
    _, request, signature, path, bundle = signed
    verifier = FileEvidenceVerifier(path)
    assert verifier(request, signature) is True
    key = bundle["keys"][0]
    now = datetime.now(UTC)
    if mode == "expired":
        key["not_after"] = (now - timedelta(minutes=1)).isoformat()
    elif mode == "future":
        key["not_before"] = (now + timedelta(minutes=1)).isoformat()
    elif mode == "removed":
        bundle["keys"] = []
    elif mode == "wrong_key":
        key["public_key"] = _b64(Ed25519PrivateKey.generate().public_key().public_bytes_raw())
    else:
        key["gate_code"] = "bb"
    path.write_text(json.dumps(bundle), encoding="utf-8")
    assert verifier(request, signature) is False


@pytest.mark.parametrize("mode", ["unknown", "duplicate_id", "duplicate_json", "oversized"])
def test_invalid_trust_is_rejected(signed, mode):
    _, request, signature, path, bundle = signed
    if mode == "unknown":
        bundle["private_key"] = "forbidden"
    elif mode == "duplicate_id":
        bundle["keys"].append(bundle["keys"][0])
    content = json.dumps(bundle)
    if mode == "duplicate_json":
        content = '{"keys":[],"keys":[]}'
    elif mode == "oversized":
        content = " " * (MAX_TRUST_BYTES + 1)
    path.write_text(content, encoding="utf-8")
    with pytest.raises(ValueError):
        FileEvidenceVerifier(path)(request, signature)


@pytest.mark.parametrize("signature", ["", "s" * 64, "_" * 86, "a" * 86 + "="])
def test_invalid_signature_encoding_is_rejected(signed, signature):
    _, request, _, path, _ = signed
    with pytest.raises(ValueError):
        FileEvidenceVerifier(path)(request, signature)


def test_missing_file_and_unconfigured_app_fail_closed(signed, monkeypatch):
    _, request, signature, path, _ = signed
    with pytest.raises(OSError):
        FileEvidenceVerifier(path.with_name("missing.json"))(request, signature)
    monkeypatch.delenv(SETTING, raising=False)
    app = SimpleNamespace(state=SimpleNamespace(config=load_app_config("control-plane-api")))
    install_launch_evidence_verifier(app)
    assert app.state.launch_evidence_verifier is None


def test_configured_path_is_private_and_production_app_installs_verifier(signed, monkeypatch):
    _, _, _, path, _ = signed
    monkeypatch.setenv(SETTING, str(path))
    config = load_app_config("control-plane-api")
    assert config.launch_evidence_trust_bundle_path == str(path)
    assert str(path) not in repr(config)
    assert str(path) not in json.dumps(config.model_dump(mode="json"))
    environment = os.environ.copy()
    environment["PYTHONPATH"] = os.pathsep.join(str(ROOT / relative) for relative in (
        "shared-sdk/python", "control-plane-api/src",
        "gateway-data-plane/src", "mcp-sandbox-worker/src",
    ))
    script = (
        "from metahub_control.app import app; "
        "from metahub_control.launch_governance.verifier import FileEvidenceVerifier; "
        "assert isinstance(app.state.launch_evidence_verifier, FileEvidenceVerifier)"
    )
    result = subprocess.run([sys.executable, "-c", script], cwd=ROOT, env=environment,
                            capture_output=True, timeout=45, check=False)
    assert result.returncode == 0, "production verifier wiring failed"
