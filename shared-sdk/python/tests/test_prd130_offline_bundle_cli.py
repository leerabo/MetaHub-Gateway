"""PRD-130 secret-safe isolated bundle verify and apply CLI tests."""

from __future__ import annotations

import base64
import io
import json
import zipfile
from datetime import UTC, datetime, timedelta

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from metahub_shared.installer.cli import run_cli
from metahub_shared.private_gateway import (
    OfflineBundleKeyEnvelope,
    OfflineDomainVersion,
    build_offline_bundle_archive,
    canonical_edge_json_bytes,
)

WORKSPACE_ID = "wsp_" + "1" * 32
TENANT_ID = "11111111-1111-4111-8111-111111111111"
DEPLOYMENT_ID = "edp_" + "2" * 32
PRIVATE_KEY = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
KEY_ID = "edge-offline-cli-v1"
CONTENT_KEY = bytes(range(32, 64))


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _bundle(path):
    now = datetime.now(UTC)
    return build_offline_bundle_archive(
        path,
        direction="cloud_to_edge",
        workspace_id=WORKSPACE_ID,
        tenant_id=TENANT_ID,
        deployment_id=DEPLOYMENT_ID,
        version_vector=(
            OfflineDomainVersion(
                domain="desired_state",
                base_generation=0,
                generation=1,
                value_sha256="3" * 64,
            ),
        ),
        entries=(("desired_state", "application/json", b'{"generation":1}'),),
        content_key=CONTENT_KEY,
        key_envelope=OfflineBundleKeyEnvelope(
            encryption_key_version=1,
            kms_key_id_sha256="4" * 64,
            wrapped_key="dGVzdC13cmFwcGVkLWtleS1tYXRlcmlhbA",
        ),
        private_key=PRIVATE_KEY,
        key_id=KEY_ID,
        created_at=now - timedelta(minutes=1),
        expires_at=now + timedelta(days=1),
    )


def _trust_store(path, signed) -> None:
    path.write_bytes(
        canonical_edge_json_bytes(
            {
                "schema_version": "metahub.edge-offline-trust/v1",
                "keys": [
                    {
                        "key_id": KEY_ID,
                        "algorithm": "Ed25519",
                        "public_key": signed.signature.public_key,
                        "purpose": "metahub.edge-offline-bundle.manifest/v1",
                        "status": "active",
                    }
                ],
            }
        )
    )


def _key_document() -> bytes:
    return canonical_edge_json_bytes(
        {
            "content_key": _b64(CONTENT_KEY),
            "schema_version": "metahub.edge-offline-media-key/v1",
        }
    )


def _key_file(path) -> None:
    path.write_bytes(_key_document())
    path.chmod(0o600)


def _arguments(source, trust, staging, *key_source: str) -> list[str]:
    return [
        "bundle",
        "verify",
        "--input",
        str(source),
        "--trust-store",
        str(trust),
        "--staging-root",
        str(staging),
        "--workspace-id",
        WORKSPACE_ID,
        "--tenant-id",
        TENANT_ID,
        "--deployment-id",
        DEPLOYMENT_ID,
        "--direction",
        "cloud_to_edge",
        *key_source,
    ]


def _apply_arguments(source, trust, staging, state, key_file) -> list[str]:
    values = _arguments(source, trust, staging, "--key-input", str(key_file))
    values[1] = "apply"
    values.extend(("--state-root", str(state), "--confirmation-stdin"))
    return values


def _assert_no_sensitive_output(output: str, source, signed) -> None:
    assert _b64(CONTENT_KEY) not in output
    assert signed.signature.value not in output
    assert signed.signature.public_key not in output
    assert signed.manifest.key_envelope_sha256 not in output
    assert str(source) not in output


def test_verify_reads_key_from_stdin_and_emits_only_fixed_summary(tmp_path) -> None:
    source = tmp_path / "offline.mhb"
    trust = tmp_path / "trust.json"
    signed = _bundle(source)
    _trust_store(trust, signed)
    stdout = io.StringIO()
    stderr = io.StringIO()

    result = run_cli(
        _arguments(source, trust, tmp_path / "staging", "--key-stdin"),
        stdin=io.StringIO(_key_document().decode("ascii")),
        stdout=stdout,
        stderr=stderr,
        require_root=False,
    )

    assert result == 0 and stderr.getvalue() == ""
    assert json.loads(stdout.getvalue()) == {
        "bundle_id": signed.manifest.bundle_id,
        "direction": "cloud_to_edge",
        "domains": ["desired_state"],
        "entry_count": 1,
        "expires_at": signed.manifest.expires_at.isoformat(timespec="milliseconds").replace(
            "+00:00", "Z"
        ),
        "manifest_sha256": signed.manifest_sha256,
        "producer_generation": 1,
        "status": "verified",
    }
    _assert_no_sensitive_output(stdout.getvalue(), source, signed)


def test_apply_requires_exact_confirmation_and_replays_as_not_modified(tmp_path) -> None:
    source = tmp_path / "offline.mhb"
    trust = tmp_path / "trust.json"
    key_file = tmp_path / "media-key.json"
    state = tmp_path / "state"
    signed = _bundle(source)
    _trust_store(trust, signed)
    _key_file(key_file)
    arguments = _apply_arguments(source, trust, tmp_path / "staging", state, key_file)
    confirmation = f"APPLY {signed.manifest.bundle_id} TO {DEPLOYMENT_ID}\n"

    first_stdout = io.StringIO()
    assert (
        run_cli(
            arguments,
            stdin=io.StringIO(confirmation),
            stdout=first_stdout,
            stderr=io.StringIO(),
            require_root=False,
        )
        == 0
    )
    assert json.loads(first_stdout.getvalue())["status"] == "applied"
    assert (state / "head.json").exists()

    replay_stdout = io.StringIO()
    assert (
        run_cli(
            arguments,
            stdin=io.StringIO(confirmation),
            stdout=replay_stdout,
            stderr=io.StringIO(),
            require_root=False,
        )
        == 0
    )
    assert json.loads(replay_stdout.getvalue())["status"] == "not_modified"
    _assert_no_sensitive_output(first_stdout.getvalue(), source, signed)
    _assert_no_sensitive_output(replay_stdout.getvalue(), source, signed)


def test_bad_signature_wrong_owner_and_missing_confirmation_fail_closed(tmp_path) -> None:
    source = tmp_path / "offline.mhb"
    trust = tmp_path / "trust.json"
    key_file = tmp_path / "media-key.json"
    signed = _bundle(source)
    _trust_store(trust, signed)
    _key_file(key_file)

    tampered = tmp_path / "tampered.mhb"
    with (
        zipfile.ZipFile(source, "r") as original,
        zipfile.ZipFile(tampered, "w", compression=zipfile.ZIP_STORED) as replacement,
    ):
        for info in original.infolist():
            value = original.read(info)
            if info.filename == "manifest.json":
                root = json.loads(value)
                signature = root["signed"]["signature"]["value"]
                root["signed"]["signature"]["value"] = (
                    "A" if signature[0] != "A" else "B"
                ) + signature[1:]
                value = canonical_edge_json_bytes(root)
            replacement.writestr(info, value)

    failures = (
        (
            _apply_arguments(
                tampered,
                trust,
                tmp_path / "tampered-stage",
                tmp_path / "tampered-state",
                key_file,
            ),
            f"APPLY {signed.manifest.bundle_id} TO {DEPLOYMENT_ID}\n",
            "edge_offline_bundle.signature_invalid",
            tmp_path / "tampered-state",
        ),
        (
            [
                *(
                    _arguments(
                        source, trust, tmp_path / "owner-stage", "--key-input", str(key_file)
                    )
                ),
            ],
            "",
            "edge_offline_bundle.owner_mismatch",
            tmp_path / "owner-state",
        ),
        (
            _apply_arguments(
                source,
                trust,
                tmp_path / "confirm-stage",
                tmp_path / "confirm-state",
                key_file,
            )[:-1],
            "",
            "installer.confirmation_required",
            tmp_path / "confirm-state",
        ),
    )
    failures[1][0][failures[1][0].index(WORKSPACE_ID)] = "wsp_" + "9" * 32

    for arguments, stdin, code, state in failures:
        stderr = io.StringIO()
        result = run_cli(
            arguments,
            stdin=io.StringIO(stdin),
            stdout=io.StringIO(),
            stderr=stderr,
            require_root=False,
        )
        assert result in {2, 4}
        assert json.loads(stderr.getvalue()) == {"status": "error", "code": code}
        assert not state.exists()
        _assert_no_sensitive_output(stderr.getvalue(), source, signed)
