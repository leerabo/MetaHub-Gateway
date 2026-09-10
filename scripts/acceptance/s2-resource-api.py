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
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[2]
BASELINE_SHA256 = "062ae8e3a8024030efb712a5120793483203b86d552fcfd86a46e9bf804f4c2a"
COMMERCIAL_MODULES = (
    "test_prd098_saas_billing", "test_prd099_platform_billing",
    "test_prd102_business_verification", "test_prd103_payment_commerce",
    "test_prd104_invoice_reconciliation", "test_prd105_notifications",
)


def prepare_commercial_database(database_url, *, work, baseline, migrations_dir):
    """Copy the verified empty schema only into a new test-owned SQLite target."""
    from metahub_shared.db import apply_migrations

    parsed = urlparse(database_url)
    if parsed.scheme != "sqlite" or parsed.netloc or parsed.query or parsed.fragment:
        raise ValueError("Commercial tests require a local SQLite database")
    raw_path = parsed.path
    if os.name == "nt" and len(raw_path) > 3 and raw_path[0] == "/" and raw_path[2] == ":":
        raw_path = raw_path[1:]
    target = Path(raw_path).resolve()
    if not target.is_relative_to(work.resolve() / "tests"):
        raise ValueError("Database target must belong to this test run")
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("xb") as destination, baseline.open("rb") as source:
        shutil.copyfileobj(source, destination)
    return apply_migrations(database_url, migrations_dir=migrations_dir)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=ROOT)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--flow", choices=("usage", "finance", "commerce", "all"), default="all")
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

    flows = ("usage", "finance", "commerce") if options.flow == "all" else (options.flow,)
    targets = []
    for flow in flows:
        names = COMMERCIAL_MODULES if flow == "commerce" else ("test_workspace_" + flow,)
        for name in names:
            module = importlib.import_module(name)
            if not Path(module.__file__).resolve().is_relative_to(source):
                raise ValueError("Imported tests do not belong to the selected source")
            if flow == "commerce":
                def prepare(database_url):
                    return prepare_commercial_database(
                        database_url, work=work, baseline=database,
                        migrations_dir=source / "infra/migrations",
                    )

                module.apply_migrations = prepare
            else:
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
