"""Fresh state must describe its actual frozen release before creating secrets."""
import hashlib
import importlib.util
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location("prepare_production_identity", ROOT / "scripts/deploy/prepare-production.py")
prepare = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prepare)


def test_frozen_release_identity_includes_frontend_manifest_bytes(tmp_path):
    manifest = tmp_path / "release-manifest.json"
    contents = json.dumps({"release": "2.0.0-trial.11", "files": [
        {"path": "console-web/src/main.js", "sha256": "a" * 64, "bytes": 1},
    ]}, indent=2).encode()
    manifest.write_bytes(contents)
    assert prepare.deployment_identity(tmp_path) == (
        "2.0.0-trial.11", "release-manifest-sha256:" + hashlib.sha256(contents).hexdigest(),
    )


def test_runtime_template_reports_verified_release_instead_of_template_default():
    template = "METAHUB_VERSION=2.0.0-trial.1\nMETAHUB_ENV=prod\n"
    assert prepare.runtime_template_for_release(template, "2.0.0-trial.43") == (
        "METAHUB_VERSION=2.0.0-trial.43\nMETAHUB_ENV=prod\n"
    )


def test_runtime_template_adds_release_when_template_omits_version():
    assert prepare.runtime_template_for_release("METAHUB_ENV=prod\n", "2.0.0-trial.43") == (
        "METAHUB_ENV=prod\nMETAHUB_VERSION=2.0.0-trial.43\n"
    )


@pytest.mark.parametrize("contents", [
    None, b"not json", b"[]", b'{}',
    b'{"release":"2.0.0-trial.11\\nOTHER=1","files":[{}]}',
    b'{"release":"2.0.0","files":[]}',
    b'{"release":3,"files":[{}]}', b" " * (4 * 1024 * 1024 + 1),
], ids=["missing", "invalid-json", "array", "empty-object", "newline-in-version", "empty-files", "numeric-version", "oversized"])
def test_invalid_bundle_cannot_partially_create_state(tmp_path, monkeypatch, contents):
    release = tmp_path / "release"
    release.mkdir()
    if contents is not None:
        (release / "release-manifest.json").write_bytes(contents)
    state = tmp_path / "new-state"
    monkeypatch.setattr(prepare, "ROOT", release)
    monkeypatch.setattr(sys, "argv", ["prepare-production.py", "--state", str(state), "--domain", "trial.example.invalid"])
    if prepare.os.name == "posix":
        monkeypatch.setattr(prepare.os, "geteuid", lambda: 0)
    monkeypatch.setattr(prepare, "openssl", lambda *args: pytest.fail("Must reject before key generation"))
    with pytest.raises((ValueError, UnicodeError)):
        prepare.main()
    assert not state.exists()


def test_existing_state_is_never_replaced_even_without_release_manifest(tmp_path, monkeypatch):
    state = tmp_path / "existing-state"
    state.mkdir()
    original = state / "existing-encryption-root"
    original.write_bytes(b"unchanged-fixture-root")
    monkeypatch.setattr(prepare, "ROOT", tmp_path)
    monkeypatch.setattr(sys, "argv", ["prepare-production.py", "--state", str(state), "--domain", "trial.example.invalid"])
    if prepare.os.name == "posix":
        monkeypatch.setattr(prepare.os, "geteuid", lambda: 0)
    with pytest.raises(ValueError, match="State path already exists"):
        prepare.main()
    assert original.read_bytes() == b"unchanged-fixture-root"
