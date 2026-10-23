"""Cold restore to fresh PostgreSQL/files volumes, preserving the original ones."""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tarfile


def load_prepare():
    spec = importlib.util.spec_from_file_location("recovery_prepare", Path(__file__).with_name("prepare-recovery.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def compose(state, release):
    command = ["docker", "compose", "--env-file", str(state / "compose.env"), "-f", str(release / "infra/production/compose.yml")]
    if (state / "recovery-volumes.json").exists():
        command += ["-f", str(state / "recovery-volumes.json")]
    return command


def fingerprint_arguments():
    # The controller supplies verification code even when restoring an old
    # matching image. Source code is public; secret values stay in mounted files.
    return [
        '-c', 'import sys; __file__=sys.argv.pop(1); exec(compile(sys.argv.pop(1), __file__, "exec"))',
        '/app/tools/database-fingerprint.py',
        Path(__file__).with_name('database-fingerprint.py').read_text(encoding='utf-8'),
    ]


def execute(command, *, timeout=7200, **kwargs):
    # Commands contain paths, fixed role names and volume identifiers, never
    # credentials. Container output remains subject to production redaction.
    child = subprocess.Popen(command, start_new_session=True, **kwargs)
    try:
        stdout, stderr = child.communicate(timeout=timeout)
    except BaseException:
        terminate_group(child)
        raise
    result = subprocess.CompletedProcess(command, child.returncode, stdout, stderr)
    result.check_returncode()
    return result


def terminate_group(child, grace=30):
    """Compose clients may leave grandchildren even after their shell exits."""
    try:
        os.killpg(child.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        child.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        pass
    finally:
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        child.wait()


def interrupted(signum, _frame):
    raise SystemExit(128 + signum)


def start_services(command):
    # The cold recovery path uses the same readiness order as install/upgrade.
    for services in (("control-api",), ("gateway",), ("console", "edge")):
        execute(command + ["up", "-d", "--no-deps", "--wait", "--wait-timeout", "300", *services])


def recover(state, prefix, target, release):
    import fcntl
    prepare = load_prepare()
    prepare.private_path(state)
    descriptor = os.open(state / "update.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if (state / "recovery-in-progress").exists():
            raise ValueError("A prior recovery needs operator review")
        active = Path((state / "current-release").read_text().strip())
        prepare.private_path(active)
        metadata = prepare.prepare(state, prefix, target, release)
        def phase(name):
            metadata["phase"] = name
            print(json.dumps({"status": "recovering", "phase": name}), flush=True)
        old, new = compose(state, active), compose(target, release)
        activation_started = False
        handlers = {signum: signal.getsignal(signum) for signum in (signal.SIGINT, signal.SIGTERM)}
        try:
            phase("configuration_check")
            execute(new + ["config", "--quiet"])
            # Resolve exact backup-release images before stopping the old deployment.
            phase("resolve_images")
            execute(new + ["pull", "postgres", "edge"])
            phase("build_images")
            execute(new + ["build", "control-api", "console"])
            # Keep this marker after activation, including on failure: an old timer
            # must never use the old state against the newly restored containers.
            with (state / "recovery-in-progress").open("x") as marker:
                marker.write(str(target) + "\n")
            activation_started = True
            phase("quiesce_original")
            execute(["bash", str(Path(__file__).with_name("metahubctl.sh")), "quiesce-upgrade", str(state)])
            execute(old + ["stop", "postgres"])
            for name in metadata["volumes"].values():
                probe = subprocess.run(["docker", "volume", "inspect", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                if probe.returncode == 0:
                    raise ValueError("Recovery volume already exists; refusing reuse")
                execute(["docker", "volume", "create", "--label", "metahub.recovery=preserve-original", name], stdout=subprocess.DEVNULL)
            phase("start_new_database")
            execute(new + ["up", "-d", "--wait", "--wait-timeout", "300", "postgres"])
            phase("restore_database")
            with (target / "recovery-database").open("rb") as dump:
                execute(new + ["exec", "-T", "--user", "postgres", "postgres", "pg_restore", "--username", "metahub_admin", "--dbname", "metahub", "--no-owner", "--role", "metahub", "--exit-on-error"], stdin=dump)
            phase("verify_database_snapshot")
            with (target / "recovery-verification").open("rb") as expected:
                execute(new + ["run", "-T", "--rm", "--no-deps", "--entrypoint", "python", "migrate"] + fingerprint_arguments() + ["--expected-stdin"], stdin=expected)
            # Import only into the freshly created files volume. Tar contents
            # have already passed the no-link/no-device validation at staging.
            phase("restore_files")
            process = subprocess.Popen(new + ["run", "-T", "--rm", "--no-deps", "--entrypoint", "tar", "control-api", "-C", "/app/.data", "-xf", "-"], stdin=subprocess.PIPE, start_new_session=True)
            try:
                with tarfile.open(fileobj=process.stdin, mode="w|") as archive:
                    archive.add(target / "recovery-files", arcname=".", recursive=True)
                process.stdin.close()
                if process.wait() != 0:
                    raise ValueError("Recovery file import failed")
            finally:
                if process.poll() is None:
                    terminate_group(process)
            # Verify file content and paths from inside the actual new volume.
            phase("verify_files")
            code = "import hashlib,json; from pathlib import Path; root=Path('/app/.data'); result={}\nfor p in sorted(root.rglob('*')):\n if p.is_file():\n  with p.open('rb') as stream: result[p.relative_to(root).as_posix()]=hashlib.file_digest(stream,'sha256').hexdigest()\nprint(json.dumps(result,sort_keys=True))"
            output = execute(new + ["run", "-T", "--rm", "--no-deps", "--entrypoint", "python", "control-api", "-c", code], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            import hashlib
            seed = target / "recovery-files"
            expected_files = {}
            for path in sorted(seed.rglob("*")):
                if path.is_file():
                    with path.open("rb") as stream:
                        expected_files[path.relative_to(seed).as_posix()] = hashlib.file_digest(stream, "sha256").hexdigest()
            if json.loads(output.stdout) != expected_files:
                raise ValueError("Restored file volume differs from backup")
            phase("application_preflight")
            execute(new + ["run", "--rm", "--no-deps", "migrate", "preflight"])
            phase("start_services")
            start_services(new)
            (target / "current-release.pending").write_text(str(release) + "\n")
            (target / "current-release.pending").replace(target / "current-release")
            metadata.update(status="recovered", database_snapshot_verified=True, file_contents_verified=True, encryption_roots_preserved=True)
            phase("complete")
        except BaseException:
            # A second interrupt must not bypass stopping daemon-owned one-offs
            # or removing plaintext. SIGKILL/power loss still needs review.
            for signum in (signal.SIGINT, signal.SIGTERM):
                signal.signal(signum, signal.SIG_IGN)
            if activation_started:
                try:
                    execute(["bash", str(Path(__file__).with_name("metahubctl.sh")), "quiesce-upgrade", str(target)], timeout=120)
                except Exception:
                    metadata["cleanup_requires_review"] = True
                finally:
                    # Also stop ordinary services if one-off inspection failed.
                    try:
                        stopped = subprocess.run(new + ["stop", "edge", "console", "gateway", "control-api"], check=False, timeout=120)
                        if stopped.returncode:
                            metadata["cleanup_requires_review"] = True
                    except Exception:
                        metadata["cleanup_requires_review"] = True
            metadata["status"] = (
                "failed_activation_requires_review" if activation_started
                else "failed_before_activation_original_running"
            )
            raise
        finally:
            try:
                # Erase plaintext even when recording the outcome fails.
                for name in ("recovery-database", "recovery-verification"):
                    (target / name).unlink(missing_ok=True)
                (target / "recovery.json").write_text(json.dumps(metadata, indent=2) + "\n")
            finally:
                for signum, handler in handlers.items():
                    signal.signal(signum, handler)
        return metadata


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("state", "prefix", "target", "release"):
        parser.add_argument("--" + name, required=True, type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, interrupted)
    try:
        if os.name != "posix" or os.geteuid() != 0:
            raise ValueError("Recovery requires root on Ubuntu")
        result = recover(*(getattr(args, name).absolute() for name in ("state", "prefix", "target", "release")))
        print(json.dumps({"status": result["status"], "original_volumes_preserved": True, "updater_reconfiguration_required": True}))
        return 0
    except Exception as error:
        print(json.dumps({"status": "failed", "phase": "deployment_recovery", "exception_type": type(error).__name__}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
