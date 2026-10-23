"""Run the backup tool as a file, without the service directory on sys.path."""

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def test_fingerprint_finds_adjacent_image_service_without_working_directory(tmp_path):
    image = tmp_path / "image"
    tools = image / "tools"
    tools.mkdir(parents=True)
    script = tools / "database-fingerprint.py"
    shutil.copyfile(ROOT / "scripts/deploy/database-fingerprint.py", script)
    # Stop at the secret-file boundary; this fixture never opens a database.
    (image / "service.py").write_text(
        "def load_secrets(path):\n"
        '    raise LookupError("private-fixture-secret-file-detail")\n'
        "def validate_production(values):\n"
        '    raise AssertionError("secret loading must finish first")\n',
        encoding="utf-8",
    )
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    result = subprocess.run(
        [sys.executable, "-I", str(script)],
        cwd=unrelated,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 1
    assert result.stdout == ""
    assert json.loads(result.stderr) == {
        "status": "failed",
        "phase": "database_verification",
        "exception_type": "LookupError",
    }
    assert "private-fixture-secret-file-detail" not in result.stderr


def test_recovery_runs_controller_tool_in_an_old_image_without_replacing_it(tmp_path):
    controller = tmp_path / "controller"
    controller.mkdir()
    for name in ("recover-deployment.py", "database-fingerprint.py"):
        shutil.copyfile(ROOT / "scripts/deploy" / name, controller / name)
    spec = importlib.util.spec_from_file_location(
        "fixture_recovery", controller / "recover-deployment.py"
    )
    recovery = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(recovery)
    arguments = recovery.fingerprint_arguments()
    assert arguments[0] == "-c"
    assert arguments[2] == "/app/tools/database-fingerprint.py"
    assert arguments[3] == (controller / "database-fingerprint.py").read_text(encoding="utf-8")

    image = tmp_path / "old-image"
    tools = image / "tools"
    tools.mkdir(parents=True)
    installed = tools / "database-fingerprint.py"
    old_contents = 'raise AssertionError("broken old image tool must not run")\n'
    installed.write_text(old_contents)
    (image / "service.py").write_text(
        "def load_secrets(path):\n"
        '    raise LookupError("private-old-image-secret-file-detail")\n'
        "def validate_production(values):\n"
        '    raise AssertionError("secret loading must finish first")\n',
        encoding="utf-8",
    )
    # Model /app under a temporary image root; run the production inline runner.
    arguments[2] = str(installed)
    result = subprocess.run(
        [sys.executable, "-I", *arguments, "--expected-stdin"],
        cwd=tmp_path,
        input='{"private": "verification fixture"}',
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 1
    assert result.stdout == ""
    assert json.loads(result.stderr) == {
        "status": "failed",
        "phase": "database_verification",
        "exception_type": "LookupError",
    }
    assert "private-old-image-secret-file-detail" not in result.stderr
    assert installed.read_text() == old_contents
