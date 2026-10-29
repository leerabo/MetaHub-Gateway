"""PRD-130 signed isolated-media bundle contract tests."""

from __future__ import annotations

import copy
import json
import zipfile
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from metahub_shared.private_gateway import (
    OfflineBundleContractError,
    OfflineBundleKeyEnvelope,
    OfflineBundleStateStore,
    OfflineDomainVersion,
    build_offline_bundle_archive,
    detect_offline_conflicts,
    reject_forbidden_offline_fields,
    verify_offline_bundle_archive,
)
from metahub_shared.private_gateway import (
    offline_bundles as offline_bundle_module,
)

WORKSPACE_ID = "wsp_" + "1" * 32
TENANT_ID = "11111111-1111-4111-8111-111111111111"
DEPLOYMENT_ID = "edp_" + "2" * 32
KEY = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
KEY_ID = "edge-offline-test-v1"
CONTENT_KEY = bytes(range(32, 64))
NOW = datetime(2026, 8, 28, 13, tzinfo=UTC)


def _envelope() -> OfflineBundleKeyEnvelope:
    return OfflineBundleKeyEnvelope(
        encryption_key_version=1,
        kms_key_id_sha256="3" * 64,
        wrapped_key="dGVzdC13cmFwcGVkLWtleS1tYXRlcmlhbA",
    )


def _versions() -> tuple[OfflineDomainVersion, ...]:
    return (
        OfflineDomainVersion(
            domain="desired_state",
            base_generation=1,
            base_sha256="4" * 64,
            generation=2,
            value_sha256="5" * 64,
        ),
        OfflineDomainVersion(
            domain="license",
            base_generation=0,
            generation=1,
            value_sha256="6" * 64,
        ),
    )


def _archive(path):
    signed = build_offline_bundle_archive(
        path,
        direction="cloud_to_edge",
        workspace_id=WORKSPACE_ID,
        tenant_id=TENANT_ID,
        deployment_id=DEPLOYMENT_ID,
        version_vector=_versions(),
        entries=(
            ("desired_state", "application/json", b'{"generation":2}'),
            ("license", "application/json", b'{"generation":1}'),
        ),
        content_key=CONTENT_KEY,
        key_envelope=_envelope(),
        private_key=KEY,
        key_id=KEY_ID,
        created_at=NOW,
        expires_at=NOW + timedelta(days=1),
    )
    return signed


def _verify(source, staging):
    return verify_offline_bundle_archive(
        source,
        staging,
        trusted_keys={KEY_ID: KEY.public_key().public_bytes_raw()},
        unwrap_key=lambda envelope: CONTENT_KEY,
        expected_workspace_id=WORKSPACE_ID,
        expected_tenant_id=TENANT_ID,
        expected_deployment_id=DEPLOYMENT_ID,
        expected_direction="cloud_to_edge",
        now=NOW + timedelta(hours=1),
    )


def _store(root) -> OfflineBundleStateStore:
    return OfflineBundleStateStore(
        root,
        workspace_id=WORKSPACE_ID,
        tenant_id=TENANT_ID,
        deployment_id=DEPLOYMENT_ID,
    )


def test_bundle_encrypts_every_entry_and_verifies_to_atomic_staging(tmp_path) -> None:
    source = tmp_path / "bundle.mhb"
    signed = _archive(source)
    assert b'{"generation":2}' not in source.read_bytes()
    verified = _verify(source, tmp_path / "staging")
    values = {
        item.domain: verified.entry_paths[item.entry_id].read_bytes()
        for item in signed.manifest.entries
    }
    assert values == {
        "desired_state": b'{"generation":2}',
        "license": b'{"generation":1}',
    }
    assert verified.staging_path.name.startswith(f"verified-{signed.manifest.bundle_id}-")
    assert _verify(source, tmp_path / "staging").staging_path == verified.staging_path


def test_bundle_streams_file_entries_before_writing_manifest(tmp_path, monkeypatch) -> None:
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    first.write_bytes(b'{"generation":2}')
    second.write_bytes(b'{"generation":1}')
    events: list[tuple[str, str]] = []

    class RecordingCipher:
        def __init__(self, key: bytes) -> None:
            assert key == CONTENT_KEY

        def encrypt(self, nonce: bytes, plaintext: bytes, aad: bytes) -> bytes:
            assert len(nonce) == 12 and aad
            events.append(("encrypt", plaintext.decode("ascii")))
            return b"x" * (len(plaintext) + 16)

    class RecordingArchive:
        def __init__(self, path, mode, *, compression, allowZip64) -> None:
            assert mode == "w" and compression == zipfile.ZIP_STORED and allowZip64 is True

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback) -> None:
            return None

        def writestr(self, info, value: bytes) -> None:
            assert value
            events.append(("write", info.filename))

    monkeypatch.setattr(offline_bundle_module, "AESGCM", RecordingCipher)
    monkeypatch.setattr(offline_bundle_module.zipfile, "ZipFile", RecordingArchive)
    output = tmp_path / "streamed.mhb"
    build_offline_bundle_archive(
        output,
        direction="cloud_to_edge",
        workspace_id=WORKSPACE_ID,
        tenant_id=TENANT_ID,
        deployment_id=DEPLOYMENT_ID,
        version_vector=_versions(),
        entries=(
            ("desired_state", "application/json", first),
            ("license", "application/json", second),
        ),
        content_key=CONTENT_KEY,
        key_envelope=_envelope(),
        private_key=KEY,
        key_id=KEY_ID,
        created_at=NOW,
        expires_at=NOW + timedelta(days=1),
    )

    assert output.exists()
    assert [kind for kind, _ in events] == ["encrypt", "write", "encrypt", "write", "write"]
    assert events[1][1].startswith("entries/")
    assert events[3][1].startswith("entries/")
    assert events[4] == ("write", "manifest.json")


def test_bundle_file_entry_failure_removes_partial_archive(tmp_path) -> None:
    valid = tmp_path / "valid.json"
    empty = tmp_path / "empty.json"
    valid.write_bytes(b'{"generation":2}')
    empty.touch()
    output = tmp_path / "partial.mhb"

    with pytest.raises(OfflineBundleContractError, match="entry_invalid"):
        build_offline_bundle_archive(
            output,
            direction="cloud_to_edge",
            workspace_id=WORKSPACE_ID,
            tenant_id=TENANT_ID,
            deployment_id=DEPLOYMENT_ID,
            version_vector=_versions(),
            entries=(
                ("desired_state", "application/json", valid),
                ("license", "application/json", empty),
            ),
            content_key=CONTENT_KEY,
            key_envelope=_envelope(),
            private_key=KEY,
            key_id=KEY_ID,
            created_at=NOW,
            expires_at=NOW + timedelta(days=1),
        )

    assert not output.exists()
    assert not tuple(tmp_path.glob(".partial.mhb.*.tmp"))


def test_bundle_rejects_actual_archive_above_media_limit(tmp_path, monkeypatch) -> None:
    original_stat = offline_bundle_module.Path.stat

    def oversized_archive_stat(path, *args, **kwargs):
        result = original_stat(path, *args, **kwargs)
        if path.name.startswith(".oversize.mhb.") and path.name.endswith(".tmp"):
            fields = list(result)
            fields[6] = offline_bundle_module.MAX_OFFLINE_BUNDLE_BYTES + 1
            return offline_bundle_module.os.stat_result(fields)
        return result

    monkeypatch.setattr(offline_bundle_module.Path, "stat", oversized_archive_stat)
    output = tmp_path / "oversize.mhb"
    with pytest.raises(OfflineBundleContractError, match="manifest_invalid"):
        build_offline_bundle_archive(
            output,
            direction="cloud_to_edge",
            workspace_id=WORKSPACE_ID,
            tenant_id=TENANT_ID,
            deployment_id=DEPLOYMENT_ID,
            version_vector=_versions(),
            entries=(("desired_state", "application/json", b"{}"),),
            content_key=CONTENT_KEY,
            key_envelope=_envelope(),
            private_key=KEY,
            key_id=KEY_ID,
            created_at=NOW,
            expires_at=NOW + timedelta(days=1),
        )

    assert not output.exists()
    assert not tuple(tmp_path.glob(".oversize.mhb.*.tmp"))


def test_bundle_rejects_wrong_owner_expiry_and_corrupt_entry(tmp_path) -> None:
    source = tmp_path / "bundle.mhb"
    _archive(source)
    with pytest.raises(OfflineBundleContractError, match="owner_mismatch"):
        verify_offline_bundle_archive(
            source,
            tmp_path / "wrong-owner",
            trusted_keys={KEY_ID: KEY.public_key().public_bytes_raw()},
            unwrap_key=lambda envelope: CONTENT_KEY,
            expected_workspace_id="wsp_" + "9" * 32,
            expected_tenant_id=TENANT_ID,
            expected_deployment_id=DEPLOYMENT_ID,
            expected_direction="cloud_to_edge",
            now=NOW,
        )
    with pytest.raises(OfflineBundleContractError, match="expired"):
        verify_offline_bundle_archive(
            source,
            tmp_path / "expired",
            trusted_keys={KEY_ID: KEY.public_key().public_bytes_raw()},
            unwrap_key=lambda envelope: CONTENT_KEY,
            expected_workspace_id=WORKSPACE_ID,
            expected_tenant_id=TENANT_ID,
            expected_deployment_id=DEPLOYMENT_ID,
            expected_direction="cloud_to_edge",
            now=NOW + timedelta(days=2),
        )
    corrupt = tmp_path / "corrupt.mhb"
    with (
        zipfile.ZipFile(source, "r") as original,
        zipfile.ZipFile(corrupt, "w", compression=zipfile.ZIP_STORED) as replacement,
    ):
        for info in original.infolist():
            value = original.read(info)
            if info.filename.startswith("entries/"):
                value = bytes([value[0] ^ 1]) + value[1:]
            replacement.writestr(info, value)
    with pytest.raises(OfflineBundleContractError, match="entry_corrupt"):
        _verify(corrupt, tmp_path / "corrupt-stage")


def test_archive_rejects_extra_path_compression_and_signature_tampering(tmp_path) -> None:
    source = tmp_path / "bundle.mhb"
    _archive(source)
    extra = tmp_path / "extra.mhb"
    with zipfile.ZipFile(source, "r") as original, zipfile.ZipFile(extra, "w") as target:
        for info in original.infolist():
            target.writestr(info, original.read(info))
        target.writestr("../outside", b"x")
    with pytest.raises(OfflineBundleContractError, match="archive_invalid"):
        _verify(extra, tmp_path / "extra-stage")

    tampered = tmp_path / "tampered.mhb"
    with zipfile.ZipFile(source, "r") as original, zipfile.ZipFile(tampered, "w") as target:
        for info in original.infolist():
            value = original.read(info)
            if info.filename == "manifest.json":
                value = value.replace(b"cloud_to_edge", b"edge_to_cloud")
            target.writestr(info, value)
    with pytest.raises(OfflineBundleContractError):
        _verify(tampered, tmp_path / "tampered-stage")


def test_version_vectors_report_dual_progress_without_last_write_wins() -> None:
    conflicts = detect_offline_conflicts(
        {"desired_state": (3, "7" * 64), "license": (0, "8" * 64)},
        _versions(),
    )
    assert len(conflicts) == 1
    assert conflicts[0].domain == "desired_state"
    assert conflicts[0].current_generation == 3
    assert conflicts[0].incoming_generation == 2

    duplicate = copy.deepcopy(_versions()[0]).model_copy(
        update={"base_generation": 2, "base_sha256": "5" * 64}
    )
    assert detect_offline_conflicts({"desired_state": (2, "5" * 64)}, (duplicate,)) == ()


def test_outbound_summary_rejects_sensitive_and_free_path_fields() -> None:
    reject_forbidden_offline_fields(
        {"status": "healthy", "usage_units": 4, "verification_code": "passed"}
    )
    for field in ("secret", "object_url", "storage_path", "prompt", "response"):
        with pytest.raises(OfflineBundleContractError, match="forbidden_field"):
            reject_forbidden_offline_fields({"summary": {field: "forbidden"}})


def test_state_store_atomically_applies_and_replays_without_changing_head(tmp_path) -> None:
    source = tmp_path / "bundle.mhb"
    signed = _archive(source)
    verified = _verify(source, tmp_path / "staging")
    state_root = tmp_path / "state"
    store = _store(state_root)

    applied = store.apply(verified)
    head_before = (state_root / "head.json").read_bytes()
    head = json.loads(head_before)
    assert applied.changed is True
    assert head["bundle_id"] == signed.manifest.bundle_id
    assert head["manifest_sha256"] == signed.manifest_sha256
    assert head["slot"] == f"slots/{signed.manifest.bundle_id}"
    assert not tuple((state_root / "slots").glob(".*.tmp"))

    replayed = store.apply(verified)
    assert replayed.changed is False
    assert (state_root / "head.json").read_bytes() == head_before


def test_state_store_recovers_slot_rename_before_head_and_receipt(tmp_path) -> None:
    source = tmp_path / "bundle.mhb"
    signed = _archive(source)
    verified = _verify(source, tmp_path / "staging")
    state_root = tmp_path / "state"
    store = _store(state_root)
    store.apply(verified)
    (state_root / "head.json").unlink()
    (state_root / "receipts" / f"{signed.manifest.bundle_id}.json").unlink()

    recovered = store.apply(verified)

    assert recovered.changed is True
    assert json.loads((state_root / "head.json").read_bytes())["bundle_id"] == (
        signed.manifest.bundle_id
    )
    assert (state_root / "receipts" / f"{signed.manifest.bundle_id}.json").exists()
    assert store.apply(verified).changed is False


def test_state_store_conflict_does_not_change_authoritative_head(tmp_path) -> None:
    source = tmp_path / "initial.mhb"
    _archive(source)
    state_root = tmp_path / "state"
    store = _store(state_root)
    store.apply(_verify(source, tmp_path / "initial-stage"))
    head_before = (state_root / "head.json").read_bytes()

    conflict_source = tmp_path / "conflict.mhb"
    build_offline_bundle_archive(
        conflict_source,
        direction="cloud_to_edge",
        workspace_id=WORKSPACE_ID,
        tenant_id=TENANT_ID,
        deployment_id=DEPLOYMENT_ID,
        version_vector=(
            OfflineDomainVersion(
                domain="desired_state",
                base_generation=1,
                base_sha256="4" * 64,
                generation=3,
                value_sha256="7" * 64,
            ),
        ),
        entries=(("desired_state", "application/json", b'{"generation":3}'),),
        content_key=CONTENT_KEY,
        key_envelope=_envelope(),
        private_key=KEY,
        key_id=KEY_ID,
        created_at=NOW,
        expires_at=NOW + timedelta(days=1),
    )
    conflict = _verify(conflict_source, tmp_path / "conflict-stage")

    with pytest.raises(OfflineBundleContractError, match="resolution_conflict"):
        store.apply(conflict)
    assert (state_root / "head.json").read_bytes() == head_before
    assert not (state_root / "slots" / conflict.verified.signed.manifest.bundle_id).exists()
