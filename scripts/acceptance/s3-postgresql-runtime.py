"""Run focused S3 acceptance in a newly created, loopback-only PostgreSQL cluster."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
PG = ROOT / ".tools/postgresql17/pgsql/bin"
TEST_FILES = (
    "control-plane-api/tests/integration/test_capacity_postgresql_runtime.py",
    "control-plane-api/tests/integration/test_platform_job_postgresql_execution.py",
    "control-plane-api/tests/integration/test_platform_job_postgresql_external.py",
)
sys.path[:0] = [str(ROOT / "shared-sdk/python"), str(ROOT / "control-plane-api/src")]

import pglast  # noqa: E402
import psycopg  # noqa: E402
from metahub_shared.db import apply_migrations, close_database_pools  # noqa: E402
from psycopg import sql  # noqa: E402


def run(work, *args, timeout=90):
    # A detached Windows postmaster can inherit pipe handles after pg_ctl exits.
    with (work / "lifecycle.log").open("ab") as output:
        return subprocess.run(
            args,
            check=True,
            timeout=timeout,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            stdout=output,
            stderr=subprocess.STDOUT,
        )


def pytest_process(args, *, env, timeout):
    with subprocess.Popen(
        args,
        cwd=ROOT,
        env=env,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    ) as process:
        try:
            return process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            # The Windows venv launcher owns a second Python process.
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    check=False,
                    timeout=15,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
            else:
                process.kill()
            process.wait(timeout=15)
            raise


def main(*, match: str | None = None) -> int:
    pglast.parse_sql("SELECT 1")
    selection = ["-k", match] if match else []
    test_env = {
        key: value for key, value in os.environ.items() if not key.startswith(("METAHUB_", "PG"))
    }
    test_env["PYTHONUTF8"] = "1"
    for key in ("ADMIN_PASSWORD", "VIRTUAL_KEY", "PROVIDER_API_KEY"):
        test_env["METAHUB_SEED_" + key] = secrets.token_urlsafe(48)
    collected = pytest_process(
        [
            sys.executable,
            "-X",
            "utf8",
            "-m",
            "pytest",
            *TEST_FILES,
            "--collect-only",
            "-q",
            *selection,
        ],
        env=test_env,
        timeout=60,
    )
    if collected != 0:
        raise RuntimeError("Native test collection failed before cluster creation.")
    work = Path(tempfile.mkdtemp(prefix="s3-capacity-native-", dir=ROOT / ".tmp")).resolve()
    assert work.parent == (ROOT / ".tmp").resolve()
    assert not (work / "data").exists()
    artifact = ROOT / ".artifacts" / work.name
    artifact.mkdir()
    report = {
        "migration": "0159",
        "status": "failed",
        "cluster_stopped": False,
        "selection": match or "all",
    }
    started = False
    began = time.monotonic()
    try:
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        run(
            work,
            str(PG / "initdb.exe"),
            "-D",
            str(work / "data"),
            "-A",
            "trust",
            "-U",
            "s3_external",
            "--no-locale",
            "-E",
            "UTF8",
            "--wal-segsize=1",
        )
        # Only this new cluster is owned, including a start that times out after spawning.
        started = True
        run(
            work,
            str(PG / "pg_ctl.exe"),
            "-D",
            str(work / "data"),
            "-l",
            str(work / "server.log"),
            "-w",
            "start",
            "-o",
            f"-h 127.0.0.1 -p {port} -c max_wal_size=128MB -c min_wal_size=16MB",
        )
        admin_url = f"postgresql://s3_external@127.0.0.1:{port}/postgres"
        name = "metahub_s3_jobs_" + uuid4().hex
        with psycopg.connect(admin_url, autocommit=True) as connection:
            connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        url = f"postgresql://s3_external@127.0.0.1:{port}/{name}"
        assert "0159" in apply_migrations(url).applied
        with psycopg.connect(url, autocommit=True) as connection:
            assert connection.execute(
                "SELECT 1 FROM schema_migrations WHERE version='0159' AND status='applied'"
            ).fetchone()
        close_database_pools()
        print("Isolated PostgreSQL schema 0159 ready; running capacity and job checks.", flush=True)
        test_env.update(
            {
                "METAHUB_TEST_POSTGRESQL_ADMIN_URL": admin_url,
                "METAHUB_TEST_JOB_POSTGRESQL_URL": url,
                "PYTHONUTF8": "1",
            }
        )
        junit = artifact / "junit.xml"
        completed = pytest_process(
            [
                sys.executable,
                "-X",
                "utf8",
                "-m",
                "pytest",
                *TEST_FILES,
                "--basetemp=" + str(work / "pytest"),
                "--junitxml=" + str(junit),
                "-q",
                "--tb=short",
                *selection,
            ],
            env=test_env,
            timeout=900,
        )
        cases = list(ET.parse(junit).getroot().iter("testcase"))
        report.update(
            {
                "tests": len(cases),
                "failures": sum(case.find("failure") is not None for case in cases),
                "errors": sum(case.find("error") is not None for case in cases),
                "skips": sum(case.find("skipped") is not None for case in cases),
            }
        )
        if (
            completed == 0
            and cases
            and not any(report[key] for key in ("failures", "errors", "skips"))
        ):
            report["status"] = "passed"
    except Exception as exc:
        report["error_type"] = type(exc).__name__
        raise
    finally:
        close_database_pools()
        try:
            if started and (work / "data/postmaster.pid").exists():
                run(
                    work,
                    str(PG / "pg_ctl.exe"),
                    "-D",
                    str(work / "data"),
                    "-m",
                    "fast",
                    "-w",
                    "stop",
                )
            report["cluster_stopped"] = not (work / "data/postmaster.pid").exists()
        finally:
            report["elapsed_seconds"] = round(time.monotonic() - began, 2)
            (artifact / "report.json").write_text(
                json.dumps(report, indent=2) + "\n", encoding="utf-8"
            )
            print(json.dumps(report), flush=True)
            print("Evidence:", artifact.relative_to(ROOT), flush=True)
    return 0 if report["status"] == "passed" and report["cluster_stopped"] else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--match", help="Optional pytest -k expression for a focused rerun.")
    raise SystemExit(main(match=parser.parse_args().match))
