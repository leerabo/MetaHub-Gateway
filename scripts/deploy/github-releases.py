"""Fetch signed GitHub release bundles into an independently configured host store.

Runs on Ubuntu using Python's standard library and OpenSSL. No network credentials
or Docker socket are exposed to the control API. Public repositories are supported.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tarfile
import tempfile
import urllib.parse
import urllib.request

VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+(?:-[a-z0-9.-]+)?")
REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
MAX_BUNDLE = 128 * 1024 * 1024
MAX_EXPANDED = 512 * 1024 * 1024


class ReleaseError(ValueError):
    pass


class GitHubRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parsed = urllib.parse.urlsplit(newurl)
        if parsed.scheme != "https" or parsed.hostname not in {
            "github.com", "api.github.com", "release-assets.githubusercontent.com",
            "objects.githubusercontent.com", "github-releases.githubusercontent.com",
        } or parsed.username or parsed.password or parsed.port not in {None, 443}:
            raise ReleaseError("Release download redirected outside GitHub HTTPS.")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def download(url, limit):
    # These downloads are public. In particular, never forward a PAT across redirects.
    request = urllib.request.Request(url, headers={
        "User-Agent": "MetaHub-release-updater", "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2026-03-10",
    })
    with urllib.request.build_opener(GitHubRedirect()).open(request, timeout=60) as response:
        content = response.read(limit + 1)
    if len(content) > limit:
        raise ReleaseError("Release download exceeds its size limit.")
    return content


def check_owned(path: Path):
    """Reject configuration, stores, or trust keys writable by application users."""
    if os.name != "posix" or os.geteuid() != 0:
        raise ReleaseError("The release updater must run as root on Ubuntu.")
    for component in (path, *path.parents):
        stat = component.lstat()
        if component.is_symlink() or stat.st_uid != 0 or stat.st_mode & 0o022:
            raise ReleaseError("Updater paths must be root-owned and not group/world writable.")


def version_order(value):
    if not VERSION.fullmatch(value):
        raise ReleaseError("Invalid version.")
    base, separator, prerelease = value.partition("-")
    parts = tuple(int(part) for part in base.split("."))
    identifiers = tuple((0, int(part)) if part.isdigit() else (1, part)
                        for part in prerelease.split(".")) if separator else ()
    return (*parts, not bool(separator), identifiers)


def verify_signature(manifest: bytes, signature: bytes, public_key: Path):
    if len(signature) != 64:
        raise ReleaseError("Invalid Ed25519 signature length.")
    with tempfile.TemporaryDirectory(prefix="metahub-signature-") as directory:
        work = Path(directory)
        (work / "manifest").write_bytes(manifest)
        (work / "signature").write_bytes(signature)
        result = subprocess.run([
            "openssl", "pkeyutl", "-verify", "-pubin", "-inkey", str(public_key),
            "-rawin", "-in", str(work / "manifest"), "-sigfile", str(work / "signature"),
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
        if result.returncode:
            raise ReleaseError("Release signature verification failed.")


def parse_manifest(data: bytes, version: str):
    value = json.loads(data)
    if not isinstance(value, dict) or set(value) != {"version", "summary", "bundle", "bytes", "sha256"}:
        raise ReleaseError("Invalid signed release manifest.")
    if not VERSION.fullmatch(version) or value["version"] != version:
        raise ReleaseError("Release tag and signed version do not match.")
    if value["bundle"] != f"metahub-{version}-source.tar.gz":
        raise ReleaseError("Unexpected bundle filename.")
    if type(value["bytes"]) is not int or not 0 < value["bytes"] <= MAX_BUNDLE:
        raise ReleaseError("Invalid signed bundle size.")
    if not isinstance(value["sha256"], str) or not re.fullmatch("[0-9a-f]{64}", value["sha256"]):
        raise ReleaseError("Invalid signed bundle digest.")
    if not isinstance(value["summary"], str) or len(value["summary"]) > 300:
        raise ReleaseError("Invalid release summary.")
    return value


def extract_verified(bundle: bytes, version: str, destination: Path):
    """No tar extractall: verify every member before creating any release file."""
    with tarfile.open(fileobj=io.BytesIO(bundle), mode="r:gz") as archive:
        entries = archive.getmembers()
        if len(entries) > 10000 or sum(entry.size for entry in entries) > MAX_EXPANDED:
            raise ReleaseError("Expanded release exceeds limits.")
        names = set()
        for entry in entries:
            path = PurePosixPath(entry.name)
            if (not entry.isfile() or path.is_absolute() or ".." in path.parts
                    or str(path) != entry.name or "\\" in entry.name or ":" in entry.name
                    or entry.name in names or entry.mode & 0o7000):
                raise ReleaseError("Unsafe or duplicate archive member.")
            names.add(entry.name)
        if "release-manifest.json" not in names:
            raise ReleaseError("Bundle file manifest is missing.")
        manifest = json.load(archive.extractfile("release-manifest.json"))
        if manifest.get("release") != version or not isinstance(manifest.get("files"), list):
            raise ReleaseError("Bundle version does not match the signed release.")
        records = {record["path"]: record for record in manifest["files"]}
        if len(records) != len(manifest["files"]) or set(records) != names - {"release-manifest.json"}:
            raise ReleaseError("Bundle file manifest does not match its files.")
        required = {"infra/production/compose.yml", "scripts/deploy/metahubctl.sh"}
        if not required.issubset(names):
            raise ReleaseError("Deployment entry points are missing.")
        # Check all bytes first. Never leave a partially validated candidate.
        contents = {}
        for entry in entries:
            data = archive.extractfile(entry).read()
            if entry.name != "release-manifest.json":
                record = records[entry.name]
                if record["bytes"] != len(data) or record["sha256"] != hashlib.sha256(data).hexdigest():
                    raise ReleaseError("Bundle file integrity verification failed.")
            contents[entry.name] = data
        destination.mkdir(mode=0o755)
        destination.chmod(0o755)
        for name, data in contents.items():
            target = destination / name
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
            # mkdir's mode is filtered by the updater's restrictive umask;
            # intermediate parents otherwise become 0700 and Docker preserves
            # those permissions, hiding packages from the non-root runtime.
            for directory in target.parents:
                if directory == destination:
                    break
                directory.chmod(0o755)
            target.write_bytes(data)
            target.chmod(0o755 if target.suffix == ".sh" else 0o644)


def atomic_json(path: Path, value, mode=0o444):
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".pending", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(value, output, ensure_ascii=False, indent=2)
            output.flush()
            os.fsync(output.fileno())
        temporary.chmod(mode)
        os.replace(temporary, path)
        if os.name == "posix":
            directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        if temporary.exists():
            temporary.chmod(0o600)
            temporary.unlink()


def asset_url(release, repository, name):
    matches = [asset for asset in release.get("assets", []) if asset.get("name") == name]
    if len(matches) != 1:
        raise ReleaseError("A required release asset is missing or ambiguous.")
    url = matches[0].get("browser_download_url", "")
    expected = f"https://github.com/{repository}/releases/download/{release['tag_name']}/{name}"
    if url != expected:
        raise ReleaseError("Unexpected release asset URL.")
    return url


def sync(config_path: Path):
    check_owned(config_path)
    config = json.loads(config_path.read_text())
    repository = config["repository"]
    if not isinstance(repository, str) or not REPOSITORY.fullmatch(repository):
        raise ReleaseError("Invalid GitHub repository.")
    store, updates, key = (Path(config[name]) for name in ("release_store", "updates", "public_key"))
    for path in (store, updates, key):
        check_owned(path)
    if not key.is_file() or not store.is_dir() or not updates.is_dir():
        raise ReleaseError("Updater paths are not prepared.")
    releases = json.loads(download(f"https://api.github.com/repos/{repository}/releases?per_page=20", 2 * 1024 * 1024))
    if not isinstance(releases, list):
        raise ReleaseError("Unexpected GitHub release response.")
    accepted = []
    for release in releases:
        tag = release.get("tag_name", "")
        version = tag[1:] if tag.startswith("v") else ""
        if (release.get("draft") or not VERSION.fullmatch(version)
                or (release.get("prerelease") and config.get("include_prereleases") is not True)):
            continue
        manifest_bytes = download(asset_url(release, repository, "metahub-release.json"), 8192)
        signature = download(asset_url(release, repository, "metahub-release.sig"), 64)
        verify_signature(manifest_bytes, signature, key)
        manifest = parse_manifest(manifest_bytes, version)
        destination = store / version
        receipt = store / f"{version}.verified.json"
        if destination.exists():
            check_owned(destination)
            check_owned(receipt)
            if json.loads(receipt.read_text())["sha256"] != manifest["sha256"]:
                raise ReleaseError("Published version changed; refusing to replace it.")
        else:
            bundle = download(asset_url(release, repository, manifest["bundle"]), manifest["bytes"])
            if len(bundle) != manifest["bytes"] or hashlib.sha256(bundle).hexdigest() != manifest["sha256"]:
                raise ReleaseError("Signed bundle integrity verification failed.")
            candidate = store / (version + ".pending")
            try:
                extract_verified(bundle, version, candidate)
                os.rename(candidate, destination)
                atomic_json(receipt, manifest)
            except Exception:
                if candidate.exists():
                    shutil.rmtree(candidate)
                raise
        accepted.append({"version": version, "summary": manifest["summary"]})
    atomic_json(updates / "catalog.json", {"repository": repository, "releases": accepted})
    return len(accepted)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    try:
        print(json.dumps({"status": "verified", "releases": sync(args.config)}))
    except Exception as error:
        # Never echo remote bodies, credentials, or subprocess output.
        print(json.dumps({"status": "failed", "type": type(error).__name__}))
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
