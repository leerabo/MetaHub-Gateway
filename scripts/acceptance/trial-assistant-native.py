"""Run the assistant suite in a disposable PG cluster; keep reports, remove stopped data."""

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from storage_guard import require_test_capacity

ROOT = Path(__file__).resolve().parents[2]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--suite",
        choices=("assistant", "background", "trigger-audit", "identity", "usage-width"),
        default="assistant",
    )
    args = parser.parse_args()
    require_test_capacity(ROOT)
    suite_file = {
        "assistant": "test_postgresql_admin_assistant.py",
        "background": "test_postgresql_background_connection_lifecycle.py",
        "trigger-audit": "test_postgresql_trigger_catalog.py",
        "identity": "test_postgresql_trigger_identity.py",
        "usage-width": "test_admin_assistant_usage_width.py",
    }[args.suite]
    work = Path(tempfile.mkdtemp(prefix=f"{args.suite}-native-", dir=ROOT / ".tmp"))
    pg = ROOT / ".tools/postgresql17/pgsql/bin"
    report_path = (
        ROOT / "output/deployment-ubuntu-20261014" / (f"{args.suite}-native-validation.json")
    )
    report = {
        "status": "in_progress",
        "platform": "Windows native PostgreSQL; supplementary",
        "work": str(work.relative_to(ROOT)),
        "customer_database_used": False,
        "postgres_cleanup_verified": False,
        "ubuntu_image_verified": False,
        "suite": args.suite,
    }
    began = time.monotonic()
    started = False

    def record():
        report["elapsed_seconds"] = round(time.monotonic() - began, 3)
        contents = json.dumps(report, indent=2)
        (work / "result.json").write_text(contents, encoding="utf-8")
        pending = report_path.with_suffix(".pending")
        pending.write_text(contents, encoding="utf-8")
        pending.replace(report_path)

    def lifecycle(program, *args):
        report["phase"] = program
        record()
        with (work / "lifecycle.log").open("ab") as output:
            subprocess.run(
                [str(pg / (program + ".exe")), *args],
                check=True,
                timeout=120,
                stdout=output,
                stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )

    record()
    try:
        lifecycle(
            "initdb",
            "-D",
            str(work / "data"),
            "-A",
            "trust",
            "-U",
            "acceptance",
            "--no-locale",
            "-E",
            "UTF8",
            "--wal-segsize=1",
        )
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        started = True
        lifecycle(
            "pg_ctl",
            "-D",
            str(work / "data"),
            "-l",
            str(work / "server.log"),
            "-w",
            "-t",
            "90",
            "start",
            "-o",
            f"-h 127.0.0.1 -p {port} "
            "-c timezone=UTC -c max_locks_per_transaction=2048 "
            "-c max_wal_size=128MB -c min_wal_size=16MB "
            "-c log_min_messages=panic -c log_min_error_statement=panic",
        )
        env = {key: value for key, value in os.environ.items() if not key.startswith("METAHUB_")}
        env.update(
            METAHUB_ENV="test",
            METAHUB_TEST_POSTGRESQL_ADMIN_URL=f"postgresql://acceptance@127.0.0.1:{port}/postgres",
            METAHUB_TEST_TRIGGER_AUDIT_PATH=str(work / "trigger-catalog.json"),
        )
        report["phase"] = "native_tests"
        record()
        with (work / "pytest.log").open("wb") as output:
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pytest",
                    "-o",
                    "addopts=",
                    "-q",
                    f"control-plane-api/tests/integration/{suite_file}",
                    "--basetemp",
                    str(work / "pytest"),
                    "--junitxml",
                    str(work / "pytest.xml"),
                ],
                cwd=ROOT,
                env=env,
                timeout=900,
                stdout=output,
                stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        cases = ET.parse(work / "pytest.xml").findall(".//testcase")
        verified = (
            result.returncode == 0
            and len(cases)
            == {"trigger-audit": 1, "identity": 4, "usage-width": 2}.get(args.suite, 4)
            and all(
                not any(case.find(tag) is not None for tag in ("failure", "error", "skipped"))
                for case in cases
            )
        )
        report.update(
            status="passed" if verified else "failed",
            test_exit_code=result.returncode,
            actual_test_cases=len(cases),
            all_native_tests_executed_and_passed=verified,
        )
        # Only failures and safe summary are read by the agent; test credentials
        # remain local to their own in-memory fixture.
        print((work / "pytest.log").read_text(errors="replace")[-18000:], flush=True)
    except Exception as error:
        report.update(status="failed", exception_type=type(error).__name__)
    finally:
        if started:
            try:
                lifecycle(
                    "pg_ctl", "-D", str(work / "data"), "-w", "-t", "90", "stop", "-m", "fast"
                )
                report["postgres_cleanup_verified"] = True
                # This exact directory was created by this invocation. Reports
                # stay outside data; never retain another full PG cluster per run.
                data = work / "data"
                if data.resolve().is_relative_to((ROOT / ".tmp").resolve()) and not (data / "postmaster.pid").exists():
                    shutil.rmtree(data)
                    report["disposable_database_removed"] = True
            except Exception as error:
                report.update(status="failed", cleanup_exception_type=type(error).__name__)
        record()
    print(json.dumps(report), flush=True)
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
