"""Actual Console DOM, browser cancellation and server revocation, without API stubs."""

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from metahub_shared.db.migrate import apply_migrations
from metahub_shared.db.seed import apply_seed_data

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(scope="module")
def browser_baseline(tmp_path_factory):
    target = tmp_path_factory.mktemp("browser-base") / "base.sqlite3"
    supplied = os.environ.get("METAHUB_TEST_MIGRATED_BASELINE")
    if supplied:
        shutil.copyfile(Path(supplied).resolve(strict=True), target)
    apply_migrations(f"sqlite:///{target.as_posix()}")
    if not supplied:
        apply_seed_data(f"sqlite:///{target.as_posix()}")
    return target


@pytest.mark.parametrize("width", [1440, 390], ids=["desktop", "mobile"])
def test_real_identity_pages_fence_delayed_memberships(
    browser_baseline, tmp_path, width, record_property
):
    node = Path(os.environ.get("METAHUB_BROWSER_TEST_NODE", r"C:\Program Files\nodejs\node.exe"))
    browser = Path(
        os.environ.get("METAHUB_BROWSER_TEST_CHROMIUM", ROOT / ".tools/chrome-portable/chrome.exe")
    )
    assert node.is_file() and browser.is_file(), "real_browser_runtime_required"
    scratch = tmp_path / "private"
    scratch.mkdir()
    before = hashlib.sha256(browser_baseline.read_bytes()).hexdigest()
    environment = {
        key: os.environ[key]
        for key in ("SYSTEMROOT", "WINDIR", "PATH", "TEMP", "TMP")
        if key in os.environ
    }
    environment.update(
        {
            "PYTHONUTF8": "1",
            "PYTHONUNBUFFERED": "1",
            "PYTHONPATH": os.pathsep.join(
                str(ROOT / path)
                for path in (
                    "control-plane-api/src",
                    "shared-sdk/python",
                    "gateway-data-plane/src",
                )
            ),
        }
    )
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "control-plane-api/tests/fixtures/prd137_browser_runtime.py"),
            str(browser_baseline),
            str(scratch),
            str(node),
            str(browser),
            str(width),
            str(tmp_path / "logged-out.png"),
        ],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        timeout=420,
        check=False,
    )
    diagnostic = result.stderr.decode("utf-8")
    assert result.stdout, diagnostic
    report = json.loads(result.stdout)
    record_property("browser_report", json.dumps(report, sort_keys=True))
    assert result.returncode == 0, report
    assert report["status"] == "passed" and report["process_exited_successfully"]
    assert report["real_authentication"] and not report["synthetic_api_responses"]
    assert report["private_directory_removed"] and report["baseline_unchanged"]
    assert all(check["passed"] for check in report["checks"])
    assert report["stderr_empty"] and not diagnostic
    assert not list(scratch.iterdir())
    assert hashlib.sha256(browser_baseline.read_bytes()).hexdigest() == before
