"""Run connection-lifecycle regression on our preserved, otherwise stopped PG cluster.

Tests create and drop only randomly named databases. No customer database is used.
This is supplementary native Windows evidence; Ubuntu still needs an image run.
"""
from pathlib import Path
import json
import os
import socket
import subprocess
import sys
import time
from uuid import uuid4
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[2]


def main():
    work = ROOT / ".tmp/trial-pg-import-4yycc3ou"
    pg_ctl = ROOT / ".tools/postgresql17/pgsql/bin/pg_ctl.exe"
    hidden = {"creationflags": subprocess.CREATE_NO_WINDOW}
    status = subprocess.run([str(pg_ctl), "-D", str(work / "data"), "status"],
                            capture_output=True, **hidden)
    if status.returncode != 3:
        raise RuntimeError("Cannot establish stopped state of task-owned cluster")
    attempt = work / ("scheduler-lifecycle-" + uuid4().hex)
    attempt.mkdir()
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]

    def lifecycle(*args):
        with (attempt / "lifecycle.log").open("ab") as log:
            subprocess.run([str(pg_ctl), "-D", str(work / "data"), *args], check=True,
                           stdout=log, stderr=log, timeout=210, **hidden)

    env = {key: value for key, value in os.environ.items() if not key.startswith("METAHUB_")}
    env.update(METAHUB_ENV="test",
               METAHUB_TEST_POSTGRESQL_ADMIN_URL=f"postgresql://acceptance@127.0.0.1:{port}/postgres")
    began = time.monotonic()
    started = False
    report_path = ROOT / "output/deployment-ubuntu-20261014/scheduler-pool-native-validation.json"
    phase = "cluster_start"
    exit_code = 1
    report = dict(status="in_progress", platform="Windows native PostgreSQL; supplementary",
                  customer_database_used=False, development_seed_used=False,
                  ubuntu_image_verified=False, attempt=str(attempt.relative_to(ROOT)),
                  postgres_cleanup_verified=False)

    def record():
        report["elapsed_seconds"] = round(time.monotonic() - began, 3)
        contents = json.dumps(report, indent=2)
        (attempt / "result.json").write_text(contents, encoding="utf-8")
        pending = report_path.with_suffix(".pending")
        pending.write_text(contents, encoding="utf-8")
        pending.replace(report_path)

    # A later timeout must not leave a previous invocation's green report current.
    record()
    try:
        started = True
        lifecycle("-l", str(attempt / "server.log"), "-w", "-t", "180", "start", "-o",
                  f"-h 127.0.0.1 -p {port} -c timezone=UTC -c max_locks_per_transaction=2048 "
                  "-c log_min_messages=panic -c log_min_error_statement=panic "
                  "-c max_wal_size=128MB -c min_wal_size=16MB")
        phase = "native_regression_tests"
        report["phase"] = phase
        record()
        command = [sys.executable, "-m", "pytest", "-o", "addopts=", "-q",
                   "control-plane-api/tests/integration/test_postgresql_background_connection_lifecycle.py",
                   "--basetemp", str(attempt / "pytest")]
        command += ["--junitxml", str(attempt / "pytest.xml")]
        with (attempt / "pytest.log").open("wb") as log:
            result = subprocess.run(command, cwd=ROOT, env=env, timeout=900,
                                    stdout=log, stderr=subprocess.STDOUT, **hidden)
        print((attempt / "pytest.log").read_text(errors="replace")[-16000:], flush=True)
        exit_code = result.returncode
        cases = ET.parse(attempt / "pytest.xml").findall(".//testcase")
        expected = {
            "test_idle_polls_and_rejected_commit_return_every_postgresql_checkout",
            "test_retired_health_scheduler_stays_read_only_on_production_schema",
            "test_platform_dashboard_can_initialize_on_unseeded_postgresql",
            "test_provider_status_native_aggregation_precision_and_contract",
        }
        verified = (exit_code == 0 and {case.get("name") for case in cases} == expected
                    and len(cases) == len(expected)
                    and all(not any(case.find(tag) is not None
                                    for tag in ("failure", "error", "skipped")) for case in cases))
        if not verified:
            exit_code = exit_code or 1
        report.update(status="passed" if exit_code == 0 else "failed",
                      full_migrated_schema=verified, repetition_count=24, pool_maximum=2,
                      test_exit_code=result.returncode, actual_test_cases=len(cases),
                      all_expected_native_tests_executed_and_passed=verified)
    except Exception as error:
        exit_code = 1
        report.update(status="failed", phase=phase, exception_type=type(error).__name__)
        print(json.dumps({"status": "failed", "phase": phase,
                          "exception_type": type(error).__name__}), flush=True)
    finally:
        if started:
            try:
                lifecycle("-w", "-t", "180", "stop", "-m", "fast")
                report["postgres_cleanup_verified"] = True
            except Exception as error:
                report.update(status="failed", cleanup_exception_type=type(error).__name__)
                exit_code = 1
        record()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
