"""Actual Windows browser-owner cleanup after the launcher has exited."""

from __future__ import annotations

import importlib.util
import json
import os
import secrets
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.parametrize(
    "script", ["prd137ConsoleEntryJourney.mjs", "prd137LateResponseJourney.mjs"]
)
def test_browser_launch_failure_emits_bounded_report(tmp_path, script):
    node = os.environ.get("METAHUB_BROWSER_TEST_NODE") or shutil.which("node")
    assert node is not None
    password = secrets.token_urlsafe(32)
    payload = {
        "origin": "https://127.0.0.1:9443",
        "spki": "A" * 43 + "=",
        "browser": str(tmp_path / "absent-browser.exe"),
        "email": "unused@example.invalid",
        "password": password,
        "workspace_id": "wsp_" + "1" * 32,
        "viewport": {"width": 390, "height": 844},
        "screenshot": str(tmp_path / "no-browser.png"),
    }
    result = subprocess.run(
        [node, str(ROOT / "tests/product_qa/web" / script)],
        input=json.dumps(payload).encode(),
        capture_output=True,
        timeout=30,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert result.returncode == 1 and not result.stderr
    assert len(result.stdout) <= 4096 and password.encode() not in result.stdout
    report = json.loads(result.stdout)
    assert report["schema"] == "metahub.browser-late-response.v1"
    assert report["status"] == "failed" and report["stage"] == "launch"
    assert report["checks"] == []
    assert not (tmp_path / "no-browser.png").exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows Job ownership contract")
def test_browser_job_reclaims_descendant_after_launcher_exit(tmp_path):
    name = "_metahub_prd137_browser_runtime_owner_test"
    specification = importlib.util.spec_from_file_location(
        name, ROOT / "control-plane-api/tests/fixtures/prd137_browser_runtime.py"
    )
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    sys.modules[name] = module
    specification.loader.exec_module(module)
    job = module._windows_child_job()
    assert job is not None
    process = None
    held_file = tmp_path / "owned-test-file"
    descendant = (
        "import sys,time; f=open(sys.argv[1], 'wb'); "
        "f.write(b'owned'); f.flush(); print('ready', flush=True); time.sleep(120)"
    )
    launcher = (
        "import subprocess,sys; "
        "assert sys.stdin.read() == 'go'; "
        "child=subprocess.Popen([sys.executable, '-c', sys.argv[1], sys.argv[2]], "
        "stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, "
        "creationflags=subprocess.CREATE_NO_WINDOW); "
        "assert child.stdout.readline().strip() == b'ready'; print('ready', flush=True)"
    )
    try:
        process = subprocess.Popen(
            [sys._base_executable, "-c", launcher, descendant, str(held_file)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        job.assign(process)
        stdout, stderr = process.communicate(b"go", timeout=15)
        assert process.returncode == 0 and stdout.strip() == b"ready" and not stderr
        assert held_file.read_bytes() == b"owned"
        assert job.active_processes() >= 1
        assert job.terminate_and_wait(5) == 0
        held_file.unlink()
        assert not held_file.exists()
    finally:
        try:
            job.terminate_and_wait(5)
        finally:
            job.close()
            if process is not None:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=5)
                for stream in (process.stdin, process.stdout, process.stderr):
                    if stream is not None:
                        stream.close()
