"""Recovery must preserve roots and refuse unsafe archives before activation."""
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import tarfile
from types import SimpleNamespace

import pytest

spec = importlib.util.spec_from_file_location("recovery_stage", Path(__file__).parents[1] / "prepare-recovery.py")
recovery = importlib.util.module_from_spec(spec)
spec.loader.exec_module(recovery)


def archive_bytes(entries):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as archive:
        for name, data, kind in entries:
            item = tarfile.TarInfo(name)
            item.type = kind
            item.mode = 0o700 if kind == tarfile.DIRTYPE else 0o600
            item.size = len(data) if kind == tarfile.REGTYPE else 0
            archive.addfile(item, io.BytesIO(data) if kind == tarfile.REGTYPE else None)
    return output.getvalue()


@pytest.mark.parametrize("name,kind", [
    ("../outside", tarfile.REGTYPE), ("/etc/passwd", tarfile.REGTYPE),
    ("backend/../../outside", tarfile.REGTYPE), ("backend\\escape", tarfile.REGTYPE),
    ("backend/link", tarfile.SYMTYPE), ("backend/hardlink", tarfile.LNKTYPE),
    ("backend/device", tarfile.CHRTYPE), ("backend/fifo", tarfile.FIFOTYPE),
])
def test_unsafe_entry_cannot_extract_any_file(tmp_path, name, kind):
    source = tmp_path / "archive.tar"
    source.write_bytes(archive_bytes([("good", b"safe", tarfile.REGTYPE), (name, b"danger", kind)]))
    destination = tmp_path / "destination"
    destination.mkdir()
    with pytest.raises(ValueError):
        recovery.extract_archive(source, destination)
    assert list(destination.iterdir()) == []


@pytest.mark.parametrize("entries", [
    [("same", b"one", tarfile.REGTYPE), ("./same", b"two", tarfile.REGTYPE)],
    [("parent", b"one", tarfile.REGTYPE), ("parent/child", b"two", tarfile.REGTYPE)],
    [("parent/child", b"two", tarfile.REGTYPE), ("parent", b"one", tarfile.REGTYPE)],
])
def test_ambiguous_paths_are_rejected(entries):
    with tarfile.open(fileobj=io.BytesIO(archive_bytes(entries))) as archive:
        with pytest.raises(ValueError):
            recovery.archive_members(archive)


def test_configuration_requires_complete_known_roots():
    for roots in (recovery.CONFIG_ROOTS - {"authority"}, recovery.CONFIG_ROOTS | {"updates"}):
        entries = [(name, b"", tarfile.DIRTYPE) for name in roots]
        with tarfile.open(fileobj=io.BytesIO(archive_bytes(entries))) as archive:
            with pytest.raises(ValueError):
                recovery.archive_members(archive, configuration=True)


def test_special_permission_bits_and_foreign_owners_are_rejected():
    for attribute, value in (("mode", 0o4755), ("uid", 1234), ("gid", 1234)):
        item = tarfile.TarInfo("file")
        setattr(item, attribute, value)
        with pytest.raises(ValueError):
            recovery.archive_members([item])


def test_only_state_path_changes_in_compose_configuration():
    contents = "METAHUB_RELEASE=2.0.0-trial.1\nMETAHUB_STATE_DIR=/opt/old\nMETAHUB_DOMAIN=fixture.invalid\nMETAHUB_TLS_POLICY=tls /run/tls/public.crt /run/tls/public.key\n"
    assert recovery.rewrite_state_directory(contents, Path("/opt/recovered")) == contents.replace("/opt/old", "/opt/recovered")
    with pytest.raises(ValueError):
        recovery.rewrite_state_directory(contents + "METAHUB_STATE_DIR=/opt/second\n", Path("/opt/new"))


@pytest.fixture
def prepared(monkeypatch, tmp_path):
    # These tests exercise payload/path preservation, not Ubuntu ownership/GPG.
    monkeypatch.setattr(recovery, "os", SimpleNamespace(name="posix", geteuid=lambda: 0, chown=lambda *args: None))
    monkeypatch.setattr(recovery, "private_path", lambda path: None)
    monkeypatch.setattr(recovery, "valid_state_spelling", lambda path: True)
    monkeypatch.setattr(recovery, "decrypt", lambda source, destination, key: shutil.copyfile(source, destination))
    if os.name == "nt":
        chmod = Path.chmod
        monkeypatch.setattr(Path, "chmod", lambda path, mode, **kwargs: chmod(path, mode | 0o600, **kwargs))
    state = tmp_path / "original"
    (state / "backups").mkdir(parents=True)
    (state / "backups/encryption.key").write_bytes(b"independent-backup-key")
    release = tmp_path / "release"
    release.mkdir()
    (release / "release-manifest.json").write_text('{"release":"2.0.0-trial.1"}')
    target = tmp_path / "new-state"
    entries = []
    payloads = {"backend/runtime.env": b"METAHUB_FILE_ENCRYPTION_SECRET=preserve-this-root\n", "postgres/app-password": b"preserved-app-password", "postgres/admin-password": b"preserved-admin-password", "authority/ca.key": b"original-ca-private-key", "backend/tls/ca.crt": b"original-ca-certificate"}
    for root in recovery.CONFIG_ROOTS:
        if root == "compose.env":
            entries.append((root, b"METAHUB_RELEASE=2.0.0-trial.1\nMETAHUB_STATE_DIR=/opt/old\nMETAHUB_DOMAIN=fixture.invalid\n", tarfile.REGTYPE))
        else:
            entries.append((root, b"", tarfile.DIRTYPE))
    entries += [(name, value, tarfile.REGTYPE) for name, value in payloads.items()]
    prefix = tmp_path / "20261015T120000Z_0123456789ab"
    artifacts = {"configuration": archive_bytes(entries), "files": archive_bytes([("documents", b"", tarfile.DIRTYPE), ("documents/encrypted.bin", b"encrypted-file-content", tarfile.REGTYPE)]), "database": b"fixture-custom-format-pg-dump", "verification": b'{"format":1}'}
    checksums = []
    for kind, data in artifacts.items():
        artifact = prefix.with_name(prefix.name + "." + kind + ".gpg")
        artifact.write_bytes(data)
        checksums.append(hashlib.sha256(data).hexdigest() + "  " + artifact.name)
    prefix.with_name(prefix.name + ".sha256").write_text("\n".join(checksums) + "\n")
    return state, prefix, target, release, payloads


def test_staging_preserves_all_root_bytes_and_creates_fresh_volume_names(prepared, monkeypatch):
    state, prefix, target, release, payloads = prepared
    # Windows path syntax is irrelevant to this Linux-only path transformation.
    monkeypatch.setattr(recovery, "rewrite_state_directory", lambda contents, path: contents.replace("/opt/old", "/opt/recovered"))
    metadata = recovery.prepare(state, prefix, target, release)
    for name, data in payloads.items():
        assert (target / name).read_bytes() == data
    assert (state / "backups/encryption.key").read_bytes() == b"independent-backup-key"
    assert (target / "backups/encryption.key").read_bytes() == b"independent-backup-key"
    assert (target / "recovery-files/documents/encrypted.bin").read_bytes() == b"encrypted-file-content"
    assert len(set(metadata["volumes"].values())) == 3
    assert all(name.startswith("metahub_recovery_") for name in metadata["volumes"].values())
    override = json.loads((target / "recovery-volumes.json").read_text())
    assert override["services"]["postgres"]["healthcheck"]["test"] == [
        "CMD", "pg_isready", "-h", "127.0.0.1", "-U", "metahub_admin", "-d", "metahub",
    ]
    assert all(item == {"external": True, "name": metadata["volumes"][name]}
               for name, item in override["volumes"].items())
    assert list((target / "updates/requests").iterdir()) == []
    assert not (target / "current-release").exists()
    assert not (state / "recovery-in-progress").exists()


def test_existing_target_is_never_overwritten(prepared):
    state, prefix, target, release, _ = prepared
    target.mkdir()
    original = target / "customer-data"
    original.write_bytes(b"keep-me")
    with pytest.raises(ValueError):
        recovery.prepare(state, prefix, target, release)
    assert original.read_bytes() == b"keep-me"


def test_wrong_backup_version_cannot_reach_activation(prepared):
    state, prefix, target, release, _ = prepared
    (release / "release-manifest.json").write_text('{"release":"2.0.0-trial.2"}')
    with pytest.raises(ValueError):
        recovery.prepare(state, prefix, target, release)
    assert not target.exists()


def test_tampered_archive_cannot_create_target(prepared):
    state, prefix, target, release, _ = prepared
    prefix.with_name(prefix.name + ".files.gpg").write_bytes(b"tampered")
    with pytest.raises(ValueError):
        recovery.prepare(state, prefix, target, release)
    assert not target.exists()
