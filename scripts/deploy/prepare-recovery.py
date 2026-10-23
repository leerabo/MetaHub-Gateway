"""Stage a complete encrypted backup in a NEW state directory for cold recovery.

Original state, database volumes and files are never overwritten. Activation is
performed by metahubctl only after this staging step has completely succeeded.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import shutil
import stat
import subprocess
import tarfile
import tempfile

CONFIG_ROOTS = frozenset({"backend", "postgres", "authority", "edge", "caddy-data", "caddy-config", "compose.env"})
MAX_MEMBERS = 100_000
MAX_CONFIG_BYTES = 128 * 1024 * 1024


def archive_members(archive, *, configuration=False):
    """Validate every entry before extracting anything; never follow links."""
    result, seen, total = [], set(), 0
    for member in archive:
        path = PurePosixPath(member.name)
        if (not member.name or path.is_absolute() or ".." in path.parts
                or "\\" in member.name or any(ord(c) < 32 for c in member.name)
                or not (member.isdir() or member.isfile())
                or member.mode & 0o7000 or member.uid not in {0, 999, 10001}
                or member.gid not in {0, 999, 10001}):
            raise ValueError("Unsafe backup archive entry")
        normalized = path.as_posix()
        if normalized == ".":
            if not member.isdir():
                raise ValueError("Invalid backup archive root")
            continue
        if normalized in seen or (configuration and path.parts[0] not in CONFIG_ROOTS):
            raise ValueError("Unexpected or duplicate backup archive entry")
        # A regular file cannot also be an ancestor of another member.
        seen.add(normalized)
        total += member.size
        result.append(member)
        if len(result) > MAX_MEMBERS or (configuration and total > MAX_CONFIG_BYTES):
            raise ValueError("Backup archive exceeds safety limits")
    files = {PurePosixPath(m.name) for m in result if m.isfile()}
    if any(any(parent in files for parent in PurePosixPath(m.name).parents) for m in result):
        raise ValueError("Backup archive contains a file as a parent")
    if configuration and {PurePosixPath(m.name).parts[0] for m in result} != CONFIG_ROOTS:
        raise ValueError("Incomplete backup configuration")
    return result


def extract_archive(source, destination, *, configuration=False):
    with tarfile.open(source, "r:") as archive:
        members = archive_members(archive, configuration=configuration)
        # All paths/types were validated, and destination is newly created and
        # private. Extract bytes ourselves: tar headers never set ownership,
        # devices, links, special bits or directories outside this destination.
        directories = []
        for member in members:
            path = destination.joinpath(*PurePosixPath(member.name).parts)
            if member.isdir():
                path.mkdir(parents=True, exist_ok=True)
                directories.append((path, member.mode & 0o777))
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            with archive.extractfile(member) as reader, path.open("xb") as writer:
                shutil.copyfileobj(reader, writer)
            path.chmod(member.mode & 0o777)
        for path, mode in sorted(directories, key=lambda item: len(item[0].parts), reverse=True):
            path.chmod(mode)


def rewrite_state_directory(contents, target):
    lines = contents.splitlines()
    matching = [line for line in lines if line.startswith("METAHUB_STATE_DIR=")]
    if len(matching) != 1 or not re.fullmatch(r"/[A-Za-z0-9_./-]+", target.as_posix()):
        raise ValueError("Invalid recovery state directory")
    return "\n".join("METAHUB_STATE_DIR=" + target.as_posix() if line.startswith("METAHUB_STATE_DIR=") else line for line in lines) + "\n"


def valid_state_spelling(path):
    return re.fullmatch(r"/[A-Za-z0-9_./-]+", path.as_posix()) is not None


def private_path(path):
    for component in (path, *path.parents):
        info = component.lstat()
        if stat.S_ISLNK(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise ValueError("Recovery paths must be root-owned and protected")


def decrypt(source, destination, key):
    with destination.open("xb") as output:
        result = subprocess.run(["gpg", "--batch", "--quiet", "--pinentry-mode", "loopback", "--passphrase-file", str(key), "--decrypt", str(source)], stdout=output, stderr=subprocess.DEVNULL)
    if result.returncode:
        raise ValueError("Encrypted backup could not be authenticated")


def prepare(state, prefix, target, release):
    if os.name != "posix" or os.geteuid() != 0:
        raise ValueError("Recovery requires root on Ubuntu")
    state, prefix, target, release = (Path(p).absolute() for p in (state, prefix, target, release))
    private_path(state)
    private_path(target.parent)
    private_path(release)
    if target.exists() or target.is_symlink() or not valid_state_spelling(target):
        raise ValueError("Recovery target must be a new protected directory")
    key = state / "backups/encryption.key"
    private_path(key)
    spec = importlib.util.spec_from_file_location("backup_verifier", Path(__file__).with_name("verify-backup.py"))
    verifier = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(verifier)
    verifier.verify(prefix)
    # Decrypt all payloads before stopping the serving deployment. The database
    # remains an authenticated custom-format dump until activation.
    with tempfile.TemporaryDirectory(prefix=".metahub-recovery-", dir=target.parent) as temporary:
        temporary = Path(temporary)
        for suffix in ("configuration", "files", "database", "verification"):
            decrypt(prefix.with_name(prefix.name + f".{suffix}.gpg"), temporary / suffix, key)
        staged = temporary / "state"
        staged.mkdir()
        extract_archive(temporary / "configuration", staged, configuration=True)
        env = (staged / "compose.env").read_text()
        versions = [line.split("=", 1)[1] for line in env.splitlines() if line.startswith("METAHUB_RELEASE=")]
        version = json.loads((release / "release-manifest.json").read_text())["release"]
        if versions != [version]:
            raise ValueError("Recovery requires the exact release recorded in the backup")
        # Do not regenerate encryption roots, TLS keys or database passwords.
        (staged / "compose.env").write_text(rewrite_state_directory(env, target), newline="\n")
        (staged / "compose.env").chmod(0o600)
        seed = staged / "recovery-files"
        seed.mkdir()
        extract_archive(temporary / "files", seed)
        (staged / "backups").mkdir(mode=0o700)
        shutil.copyfile(key, staged / "backups/encryption.key")
        (staged / "backups/encryption.key").chmod(0o400)
        for suffix in ("database", "verification"):
            shutil.move(str(temporary / suffix), staged / ("recovery-" + suffix))
            (staged / ("recovery-" + suffix)).chmod(0o600)
        # Restoring a stale upgrade queue could replay an old migration. Start
        # with an empty queue and require the operator to reconfigure the worker.
        for directory in ("updates", "updates/requests", "updates/status"):
            (staged / directory).mkdir(mode=0o755)
        (staged / "updates/catalog.json").write_text('{"releases":[]}\n')
        (staged / "updates/catalog.json").chmod(0o444)
        os.chown(staged / "updates/requests", 10001, 10001)
        identifier = secrets.token_hex(12)
        volumes = {name: "metahub_recovery_" + identifier + "_" + name for name in ("postgres", "files", "pg_socket")}
        override = {
            "volumes": {name: {"external": True, "name": value} for name, value in volumes.items()},
            # A matching older release may probe the temporary init server's
            # socket. Restore only after the final TCP server has started.
            "services": {"postgres": {"healthcheck": {"test": [
                "CMD", "pg_isready", "-h", "127.0.0.1", "-U", "metahub_admin", "-d", "metahub",
            ]}}},
        }
        (staged / "recovery-volumes.json").write_text(json.dumps(override) + "\n")
        (staged / "recovery-volumes.json").chmod(0o600)
        metadata = {"format": 1, "status": "staged", "release": version, "release_root": str(release), "source_state": str(state), "target_state": str(target), "volumes": volumes, "original_volumes_preserved": True, "updater_reconfiguration_required": True}
        (staged / "recovery.json").write_text(json.dumps(metadata, indent=2) + "\n")
        (staged / "recovery.json").chmod(0o600)
        for directory, uid in (("backend", 10001), ("postgres", 999), ("edge", 10001), ("caddy-data", 10001), ("caddy-config", 10001)):
            base = staged / directory
            for path in (base, *base.rglob("*")):
                os.chown(path, uid, uid)
        staged.chmod(0o711)
        staged.rename(target)
    return metadata


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("state", "prefix", "target", "release"):
        parser.add_argument("--" + name, required=True, type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    try:
        result = prepare(args.state, args.prefix, args.target, args.release)
        print(json.dumps({"status": "staged", "release": result["release"], "original_data_modified": False}))
        return 0
    except Exception as error:
        print(json.dumps({"status": "failed", "phase": "recovery_staging", "exception_type": type(error).__name__}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
