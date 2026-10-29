import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tarfile

import pytest

ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location("signed_releases", ROOT / "scripts/deploy/github-releases.py")
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


def archive(records, extra=None):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz") as output:
        files = []
        for name, data in records.items():
            item = tarfile.TarInfo(name)
            item.size = len(data)
            output.addfile(item, io.BytesIO(data))
            files.append({"path": name, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()})
        manifest = json.dumps({"release": "2.0.0-trial.2", "files": files}).encode()
        item = tarfile.TarInfo("release-manifest.json")
        item.size = len(manifest)
        output.addfile(item, io.BytesIO(manifest))
        if extra:
            output.addfile(extra)
    return stream.getvalue()


def test_atomic_status_write_survives_an_old_pending_file(tmp_path):
    target=tmp_path / 'status.json'
    old_pending=tmp_path / 'status.json.pending'
    old_pending.write_text('interrupted previous write')
    release.atomic_json(target,{'status':'completed'})
    assert json.loads(target.read_text(encoding='utf-8'))=={'status':'completed'}
    assert old_pending.read_text()=='interrupted previous write'


def test_failed_atomic_status_write_preserves_previous_record(tmp_path):
    target=tmp_path / 'status.json'
    release.atomic_json(target,{'status':'running'})
    before=target.read_bytes()
    with pytest.raises(TypeError):
        release.atomic_json(target,{'unserializable':{1,2}})
    assert target.read_bytes()==before
    assert not list(tmp_path.glob('.status.json.*.pending'))


def test_verified_bundle_installs_only_manifest_files(tmp_path):
    data = archive({"infra/production/compose.yml": b"services: {}", "scripts/deploy/metahubctl.sh": b"exit 0"})
    release.extract_verified(data, "2.0.0-trial.2", tmp_path / "candidate")
    assert (tmp_path / "candidate/scripts/deploy/metahubctl.sh").read_bytes() == b"exit 0"


@pytest.mark.skipif(os.name != "posix", reason="POSIX traversal permissions")
def test_restrictive_updater_umask_keeps_nested_source_traversable(tmp_path):
    data = archive({
        "infra/production/compose.yml": b"services: {}",
        "scripts/deploy/metahubctl.sh": b"exit 0",
        "shared-sdk/python/metahub_shared/runtime/config.py": b"VALUE = 1",
    })
    destination = tmp_path / "candidate"
    previous = os.umask(0o077)
    try:
        release.extract_verified(data, "2.0.0-trial.2", destination)
    finally:
        os.umask(previous)
    for directory in (destination, *(p for p in destination.rglob("*") if p.is_dir())):
        assert directory.stat().st_mode & 0o777 == 0o755, directory
    assert (destination / "shared-sdk/python/metahub_shared/runtime/config.py").stat().st_mode & 0o777 == 0o644


@pytest.mark.parametrize("name,kind", [("../escape", tarfile.REGTYPE), ("/absolute", tarfile.REGTYPE), ("scripts/link", tarfile.SYMTYPE)])
def test_archive_escape_and_links_are_rejected_before_extraction(tmp_path, name, kind):
    item = tarfile.TarInfo(name)
    item.type = kind
    item.linkname = "../../escape"
    data = archive({"infra/production/compose.yml": b"x", "scripts/deploy/metahubctl.sh": b"x"}, item)
    with pytest.raises(release.ReleaseError):
        release.extract_verified(data, "2.0.0-trial.2", tmp_path / "candidate")
    assert not (tmp_path / "candidate").exists()


def test_signed_version_must_match_github_tag():
    manifest = {"version": "2.0.0-trial.2", "summary": "test", "bundle": "metahub-2.0.0-trial.2-source.tar.gz", "bytes": 100, "sha256": "a"*64}
    with pytest.raises(release.ReleaseError):
        release.parse_manifest(json.dumps(manifest).encode(), "2.0.0-trial.1")


def test_asset_cannot_point_to_a_different_repository():
    value = {"tag_name": "v2.0.0", "assets": [{"name": "metahub-release.json", "browser_download_url": "https://github.com/attacker/app/releases/download/v2.0.0/metahub-release.json"}]}
    with pytest.raises(release.ReleaseError):
        release.asset_url(value, "leerabo/MetaHub", "metahub-release.json")


def test_numeric_prereleases_and_stable_versions_sort_correctly():
    order = release.version_order
    assert order("2.0.0-trial.2") < order("2.0.0-trial.10") < order("2.0.0") < order("2.0.1")


def test_external_redirect_is_rejected():
    with pytest.raises(release.ReleaseError):
        release.GitHubRedirect().redirect_request(None, None, 302, "", {}, "https://attacker.invalid/package")
