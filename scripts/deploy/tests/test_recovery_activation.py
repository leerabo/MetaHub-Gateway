"""Cold recovery failures must retain original volumes and erase plaintext staging."""
import importlib.util
import json
from pathlib import Path
import signal
import subprocess
import sys
from types import SimpleNamespace

import pytest

spec = importlib.util.spec_from_file_location(
    "deployment_recovery", Path(__file__).parents[1] / "recover-deployment.py",
)
recovery = importlib.util.module_from_spec(spec)
spec.loader.exec_module(recovery)


@pytest.mark.parametrize("failure", [None, "control-api", "gateway", "console"])
def test_recovery_waits_for_each_backend_before_exposing_frontends(monkeypatch, failure):
    calls = []
    def execute(command):
        services = command[command.index("300") + 1:]
        calls.append(services)
        assert command[:2] == ["docker", "compose"]
        assert command[2:8] == ["up", "-d", "--no-deps", "--wait", "--wait-timeout", "300"]
        if failure in services:
            raise subprocess.CalledProcessError(1, ["safe-test-command"])
    monkeypatch.setattr(recovery, "execute", execute)
    if failure:
        with pytest.raises(subprocess.CalledProcessError):
            recovery.start_services(["docker", "compose"])
    else:
        recovery.start_services(["docker", "compose"])
    expected = [["control-api"], ["gateway"], ["console", "edge"]]
    count = {"control-api": 1, "gateway": 2, "console": 3, None: 3}[failure]
    assert calls == expected[:count]


@pytest.mark.parametrize("failure", ["config", "pull", "build", "quiesce", "restore", "interrupt-build", "interrupt-restore"])
def test_failed_recovery_has_safe_failure_boundary(tmp_path, monkeypatch, failure):
    state, target, release = [tmp_path / name for name in ("old-state", "new-state", "release")]
    state.mkdir()
    release.mkdir()
    (state / "current-release").write_text(str(release))
    (state / "original-data").write_bytes(b"preserve original business data")
    commands = []
    handlers = {signum: signal.getsignal(signum) for signum in (signal.SIGINT, signal.SIGTERM)}

    def stage(*_args):
        target.mkdir()
        for name in ("recovery-database", "recovery-verification"):
            (target / name).write_bytes(b"private plaintext staging")
        return {"status": "staged", "volumes": {"postgres": "fresh-only-volume"}}

    monkeypatch.setattr(recovery, "load_prepare", lambda: SimpleNamespace(
        private_path=lambda _path: None, prepare=stage,
    ))
    monkeypatch.setitem(sys.modules, "fcntl", SimpleNamespace(
        LOCK_EX=1, LOCK_NB=2, flock=lambda *_args: None,
    ))
    # Windows cannot create O_NOFOLLOW; no filesystem security is claimed here.
    monkeypatch.setattr(recovery.os, "O_NOFOLLOW", 0, raising=False)

    def execute(command, **_kwargs):
        commands.append(command)
        phase = ("quiesce" if "quiesce-upgrade" in command else
                 "restore" if "pg_restore" in command else
                 next((name for name in ("config", "pull", "build") if name in command), None))
        if phase == failure:
            raise subprocess.CalledProcessError(1, ["redacted-test-command"])
        if failure == "interrupt-" + str(phase):
            raise SystemExit(143)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(recovery, "execute", execute)

    def run(command, **_kwargs):
        commands.append(command)
        return SimpleNamespace(returncode=1 if "inspect" in command else 0)

    monkeypatch.setattr(recovery.subprocess, "run", run)
    expected_error = SystemExit if failure.startswith("interrupt-") else subprocess.CalledProcessError
    with pytest.raises(expected_error):
        recovery.recover(state, tmp_path / "backup-prefix", target, release)
    assert (state / "original-data").read_bytes() == b"preserve original business data"
    assert not (target / "current-release").exists()
    assert not any("down" in command or "rm" in command or "prune" in command
                   for command in commands)
    assert all(not (target / name).exists()
               for name in ("recovery-database", "recovery-verification"))
    metadata = json.loads((target / "recovery.json").read_text())
    assert all(signal.getsignal(signum) == handler for signum, handler in handlers.items())
    if failure in {"config", "pull", "build", "interrupt-build"}:
        assert metadata["status"] == "failed_before_activation_original_running"
        assert not (state / "recovery-in-progress").exists()
        assert not any("stop" in command or "quiesce-upgrade" in command for command in commands)
    else:
        assert metadata["status"] == "failed_activation_requires_review"
        assert (state / "recovery-in-progress").read_text().strip() == str(target)
        assert any("quiesce-upgrade" in command and str(target) in command for command in commands)
        assert commands[-1][-5:] == ["stop", "edge", "console", "gateway", "control-api"]
