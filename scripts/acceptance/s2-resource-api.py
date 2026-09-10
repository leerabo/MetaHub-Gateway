"""Run a business batch against isolated copies of a verified migration baseline."""

import argparse
import hashlib
import importlib
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BASELINE_SHA256 = "062ae8e3a8024030efb712a5120793483203b86d552fcfd86a46e9bf804f4c2a"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=ROOT)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--flow", choices=("usage", "finance", "all"), default="all")
    options, pytest_arguments = parser.parse_known_args()
    source = options.source.resolve(strict=True)
    baseline = options.baseline.resolve(strict=True)
    if source != ROOT and not source.is_relative_to(ROOT / ".tmp"):
        raise ValueError("Source must be the workspace or an isolated export")
    if not baseline.is_relative_to(ROOT / ".tmp"):
        raise ValueError("Baseline must be an isolated test database")
    with baseline.open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != BASELINE_SHA256:
            raise ValueError("Unverified migration baseline")
    if shutil.disk_usage(ROOT).free < 512 * 1024 * 1024:
        raise ValueError("Insufficient space for isolated API tests")
    work = Path(tempfile.mkdtemp(prefix="s2-api-", dir=ROOT / ".tmp"))
    evidence = ROOT / ".artifacts" / work.name
    evidence.mkdir()
    if os.name == "nt":
        subprocess.run(["compact.exe", "/C", str(work)], check=True, capture_output=True)
    database = work / "baseline.sqlite3"
    shutil.copy2(baseline, database)
    os.chdir(work)
    logging.disable(logging.CRITICAL)
    sys.path[:0] = [
        str(source / part)
        for part in (
            "shared-sdk/python",
            "control-plane-api/src",
            "gateway-data-plane/src",
            "control-plane-api/tests/integration",
        )
    ]
    for key in tuple(os.environ):
        if key.startswith("METAHUB_"):
            os.environ.pop(key)
    os.environ["METAHUB_SEED_FILE"] = str(source / "infra/seeds/default_seed.json")
    import pytest
    from metahub_shared.db import apply_migrations

    migrations = apply_migrations(
        "sqlite:///" + database.as_posix(),
        migrations_dir=source / "infra/migrations",
    )

    @pytest.fixture(scope="module")
    def migrated_database():
        return database

    flows = ("usage", "finance") if options.flow == "all" else (options.flow,)
    targets = []
    for flow in flows:
        module = importlib.import_module("test_workspace_" + flow)
        if not Path(module.__file__).resolve().is_relative_to(source):
            raise ValueError("Imported tests do not belong to the selected source")
        module.migrated_database = migrated_database
        targets.append(str(Path(module.__file__).resolve()))
    report = {
        "source": str(source),
        "flow": options.flow,
        "baseline_sha256": BASELINE_SHA256,
        "applied_migrations": migrations.applied,
        "provider_calls": 0,
    }
    print(json.dumps({"evidence": str(evidence), **report}), flush=True)
    status = pytest.main(
        [
            *targets,
            "-q",
            "--show-capture=no",
            "--tb=short",
            "--basetemp=" + str(work / "tests"),
            "--junitxml=" + str(evidence / "results.xml"),
            *pytest_arguments,
        ]
    )
    report["exit_code"] = int(status)
    (evidence / "run.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return int(status)


if __name__ == "__main__":
    raise SystemExit(main())
