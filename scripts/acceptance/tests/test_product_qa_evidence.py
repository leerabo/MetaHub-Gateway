"""Source and output binding tests; synthetic manifests never count as QA runs."""

import json
import subprocess

import pytest

from scripts.quality import product_qa_evidence as evidence

SOURCE = {"source_revision": "a" * 40, "source_sha256": "b" * 64, "source_file_count": 3}


def test_snapshot_detects_dirty_new_and_deleted_source_but_excludes_ignored_secrets(tmp_path):
    def git(*args):
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)

    git("init", "-q")
    directory = tmp_path / "console-web"
    directory.mkdir()
    source = directory / "app.js"
    source.write_text("export const x = 1;", encoding="utf-8")
    (tmp_path / ".gitignore").write_text(".env\n", encoding="utf-8")
    git("add", ".")
    git("-c", "user.name=QA", "-c", "user.email=qa@example.invalid", "commit", "-qm", "fixture")
    baseline = evidence.source_snapshot(tmp_path)
    (directory / ".env").write_text("SECRET=synthetic-test-value", encoding="utf-8")
    assert evidence.source_snapshot(tmp_path) == baseline
    source.write_text("export const x = 2;", encoding="utf-8")
    changed = evidence.source_snapshot(tmp_path)
    assert changed["source_revision"] == baseline["source_revision"]
    assert changed["source_sha256"] != baseline["source_sha256"]
    (directory / "new.js").write_text("export const y = 1;", encoding="utf-8")
    new = evidence.source_snapshot(tmp_path)
    assert new["source_sha256"] != changed["source_sha256"]
    source.unlink()
    assert evidence.source_snapshot(tmp_path)["source_sha256"] != new["source_sha256"]


@pytest.fixture
def bundle(tmp_path, monkeypatch):
    monkeypatch.setattr(evidence, "source_snapshot", lambda: SOURCE)
    (tmp_path / "summary.json").write_text('{"passed":true}', encoding="utf-8")
    manifest = evidence.seal_evidence(tmp_path, SOURCE)
    (tmp_path / "evidence-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return tmp_path, manifest


def test_manifest_binds_exact_artifact_bytes_and_required_files(bundle):
    directory, _ = bundle
    assert evidence.verify_evidence(directory, {"summary.json"}, SOURCE)
    assert not evidence.verify_evidence(directory, {"missing.json"}, SOURCE)
    (directory / "summary.json").write_text('{"passed":false}', encoding="utf-8")
    assert not evidence.verify_evidence(directory, {"summary.json"}, SOURCE)


@pytest.mark.parametrize("mutation", ("source_start", "source_finish", "source_unchanged", "path"))
def test_manifest_rejects_source_drift_and_external_paths(bundle, mutation):
    directory, manifest = bundle
    if mutation == "path":
        manifest["artifacts"]["../outside.json"] = "c" * 64
    elif mutation == "source_unchanged":
        manifest[mutation] = False
    else:
        manifest[mutation] = {**SOURCE, "source_sha256": "c" * 64}
    (directory / "evidence-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    assert not evidence.verify_evidence(directory, {"summary.json"}, SOURCE)


def test_seal_rejects_a_source_change_during_execution(tmp_path, monkeypatch):
    monkeypatch.setattr(evidence, "source_snapshot", lambda: {**SOURCE, "source_sha256": "c" * 64})
    assert evidence.seal_evidence(tmp_path, SOURCE)["source_unchanged"] is False
