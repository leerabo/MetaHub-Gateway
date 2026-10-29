"""Safety checks for the isolated native-capacity acceptance launcher."""

import importlib
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def runner(monkeypatch):
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.syspath_prepend(str(ROOT / "scripts/acceptance"))
    return importlib.import_module("s3-postgresql-runtime")


@pytest.mark.parametrize("fails", [False, True])
def test_lifecycle_output_uses_a_closed_file_not_inherited_pipe(
    runner, monkeypatch, tmp_path, fails
):
    observed = {}

    def command(args, **kwargs):
        output = kwargs["stdout"]
        observed["output"] = output
        assert not output.closed
        assert output.name == str(tmp_path / "lifecycle.log")
        assert kwargs["stderr"] == subprocess.STDOUT
        assert kwargs["timeout"] == 90
        assert kwargs["check"] is True
        output.write(b"synthetic lifecycle output\n")
        if fails:
            raise subprocess.TimeoutExpired(args, 90)
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(runner.subprocess, "run", command)
    if fails:
        with pytest.raises(subprocess.TimeoutExpired):
            runner.run(tmp_path, "synthetic-pg-control")
    else:
        assert runner.run(tmp_path, "synthetic-pg-control").returncode == 0
    assert observed["output"].closed
    assert (tmp_path / "lifecycle.log").read_bytes() == b"synthetic lifecycle output\n"


def test_parser_preflight_fails_before_cluster_creation(runner, monkeypatch):
    def unavailable(_sql):
        raise RuntimeError("synthetic parser unavailable")

    monkeypatch.setattr(runner.pglast, "parse_sql", unavailable)
    monkeypatch.setattr(
        runner.tempfile, "mkdtemp", lambda **kw: pytest.fail("cluster created before preflight")
    )
    with pytest.raises(RuntimeError, match="synthetic parser unavailable"):
        runner.main()


def test_collection_preflight_is_sanitized_and_cannot_create_cluster(runner, monkeypatch):
    monkeypatch.setenv("METAHUB_DATABASE_URL", "synthetic-out-of-scope-url")
    monkeypatch.setenv("METAHUB_SEED_FILE", "synthetic-out-of-scope-file")
    monkeypatch.setenv("METAHUB_SEED_PROVIDER_API_KEY", "synthetic-out-of-scope-secret")
    monkeypatch.setenv("PGSERVICEFILE", "synthetic-out-of-scope-pg-config")
    monkeypatch.setattr(
        runner.tempfile, "mkdtemp", lambda **kw: pytest.fail("cluster created before preflight")
    )

    def collect(args, **kwargs):
        env = kwargs["env"]
        assert "--collect-only" in args
        assert "METAHUB_DATABASE_URL" not in env
        assert "METAHUB_SEED_FILE" not in env
        assert "PGSERVICEFILE" not in env
        assert env["METAHUB_SEED_PROVIDER_API_KEY"] != "synthetic-out-of-scope-secret"
        raise subprocess.CalledProcessError(2, args)

    monkeypatch.setattr(runner, "pytest_process", collect)
    with pytest.raises(subprocess.CalledProcessError):
        runner.main()


@pytest.mark.parametrize("times_out", [False, True])
def test_pytest_child_wait_and_owned_tree_timeout(runner, monkeypatch, times_out):
    actions = []

    class Child:
        pid = 12345

        def __enter__(self):
            return self

        def __exit__(self, *_):
            actions.append("closed")

        def wait(self, *, timeout):
            actions.append(("wait", timeout))
            if times_out and timeout == 900:
                raise subprocess.TimeoutExpired("synthetic-pytest", timeout)
            return 0

        def kill(self):
            actions.append("killed")

    def popen(args, **kwargs):
        assert args == ["synthetic-pytest"]
        assert kwargs["env"] == {"SYNTHETIC": "only"}
        return Child()

    def terminate(args, **kwargs):
        assert args == ["taskkill", "/PID", "12345", "/T", "/F"]
        assert kwargs["timeout"] == 15
        actions.append("killed")

    monkeypatch.setattr(runner.subprocess, "Popen", popen)
    monkeypatch.setattr(runner.subprocess, "run", terminate)
    if times_out:
        with pytest.raises(subprocess.TimeoutExpired):
            runner.pytest_process(["synthetic-pytest"], env={"SYNTHETIC": "only"}, timeout=900)
        assert actions == [("wait", 900), "killed", ("wait", 15), "closed"]
    else:
        assert (
            runner.pytest_process(["synthetic-pytest"], env={"SYNTHETIC": "only"}, timeout=900) == 0
        )
        assert actions == [("wait", 900), "closed"]


@pytest.mark.skipif(os.name != "nt", reason="Windows venv process-tree regression")
def test_real_windows_timeout_stops_owned_python_tree(runner, monkeypatch):
    import psutil

    original = subprocess.run
    observed = []

    def terminate(args, **kwargs):
        assert args[0] == "taskkill"
        parent = psutil.Process(int(args[2]))
        observed.extend([parent, *parent.children(recursive=True)])
        return original(args, **kwargs)

    monkeypatch.setattr(runner.subprocess, "run", terminate)
    with pytest.raises(subprocess.TimeoutExpired):
        runner.pytest_process(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            env={"SystemRoot": os.environ["SystemRoot"], "PYTHONUTF8": "1"},
            timeout=2,
        )
    assert len(observed) >= 2
    assert all(not process.is_running() for process in observed)
