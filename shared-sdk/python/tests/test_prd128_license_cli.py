"""PRD-128 secret-safe offline license import CLI tests."""

from __future__ import annotations

import copy
import io
import json
from datetime import UTC, datetime, timedelta

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from metahub_shared.installer.cli import run_cli
from metahub_shared.private_gateway import (
    LicenseCapacity,
    LicenseEntitlement,
    canonical_edge_json_bytes,
    sign_license_entitlement,
)

WORKSPACE_ID = "wsp_" + "1" * 32
TENANT_ID = "11111111-1111-4111-8111-111111111111"
DEPLOYMENT_ID = "edp_" + "2" * 32
KEY = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
KEY_ID = "edge-license-cli-v1"
NOW = datetime.now(UTC)


def _signed():
    expires_at = NOW + timedelta(days=30)
    return sign_license_entitlement(
        LicenseEntitlement(
            entitlement_id="let_" + "3" * 32,
            license_id="lic_" + "4" * 32,
            workspace_id=WORKSPACE_ID,
            tenant_id=TENANT_ID,
            deployment_id=DEPLOYMENT_ID,
            device_generation=1,
            commercial_source_type="entitlement_snapshot",
            commercial_source_id="ent_" + "5" * 32,
            commercial_source_revision=1,
            generation=1,
            refresh_counter=1,
            revocation_generation=0,
            status="active",
            capacity=LicenseCapacity(
                node_limit=3,
                concurrent_request_limit=20,
                features=["advanced_routing"],
            ),
            issued_at=NOW,
            not_before=NOW,
            expires_at=expires_at,
            grace_ends_at=expires_at + timedelta(days=7),
        ),
        private_key=KEY,
        key_id=KEY_ID,
    )


def _trust_store(path) -> None:
    path.write_bytes(
        canonical_edge_json_bytes(
            {
                "schema_version": "metahub.edge-license-trust/v1",
                "keys": [
                    {
                        "key_id": KEY_ID,
                        "algorithm": "Ed25519",
                        "public_key": _signed().signature.public_key,
                        "purpose": "metahub.edge-license.entitlement/v1",
                        "status": "active",
                    }
                ],
            }
        )
    )


def _arguments(state_root, trust_store, *source: str) -> list[str]:
    return [
        "license",
        "import",
        *source,
        "--trust-store",
        str(trust_store),
        "--state-root",
        str(state_root),
        "--workspace-id",
        WORKSPACE_ID,
        "--tenant-id",
        TENANT_ID,
        "--deployment-id",
        DEPLOYMENT_ID,
        "--device-generation",
        "1",
    ]


def test_private_file_import_verifies_then_applies_without_signature_output(tmp_path) -> None:
    signed = _signed()
    source = tmp_path / "license.json"
    trust = tmp_path / "trust.json"
    state = tmp_path / "state"
    source.write_bytes(canonical_edge_json_bytes(signed.model_dump(mode="json")))
    _trust_store(trust)
    stdout = io.StringIO()
    stderr = io.StringIO()
    result = run_cli(
        _arguments(state, trust, "--input", str(source)),
        stdout=stdout,
        stderr=stderr,
        require_root=False,
    )
    assert result == 0 and stderr.getvalue() == ""
    summary = json.loads(stdout.getvalue())
    assert summary["status"] == "applied"
    assert summary["generation"] == 1
    assert summary["decision_status"] == "current"
    assert "signature" not in stdout.getvalue()
    assert "public_key" not in stdout.getvalue()
    assert (state / "head.json").exists()


def test_stdin_import_and_invalid_signature_never_create_candidate_state(tmp_path) -> None:
    signed = _signed()
    trust = tmp_path / "trust.json"
    _trust_store(trust)
    stdin_state = tmp_path / "stdin-state"
    stdout = io.StringIO()
    assert (
        run_cli(
            _arguments(stdin_state, trust, "--stdin"),
            stdin=io.StringIO(
                canonical_edge_json_bytes(signed.model_dump(mode="json")).decode("ascii")
            ),
            stdout=stdout,
            stderr=io.StringIO(),
            require_root=False,
        )
        == 0
    )

    tampered = copy.deepcopy(signed.model_dump(mode="json"))
    value = tampered["signature"]["value"]
    tampered["signature"]["value"] = ("A" if value[0] != "A" else "B") + value[1:]
    invalid_state = tmp_path / "invalid-state"
    stderr = io.StringIO()
    result = run_cli(
        _arguments(invalid_state, trust, "--stdin"),
        stdin=io.StringIO(canonical_edge_json_bytes(tampered).decode("ascii")),
        stdout=io.StringIO(),
        stderr=stderr,
        require_root=False,
    )
    assert result == 4
    assert json.loads(stderr.getvalue()) == {
        "status": "error",
        "code": "edge_license.signature_invalid",
    }
    assert not invalid_state.exists()
