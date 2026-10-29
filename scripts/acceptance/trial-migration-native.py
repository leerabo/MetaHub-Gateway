"""Additional native PostgreSQL import check; does not replace Ubuntu acceptance."""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "shared-sdk/python"))
from metahub_shared.db import apply_migrations, close_database_pools
import psycopg
from storage_guard import require_test_capacity


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--resume-work", type=Path, help="Reuse a stopped, task-owned PostgreSQL acceptance cluster without replacing its data.")
    args = parser.parse_args()
    require_test_capacity(ROOT)
    # Import verification needs a fresh database, but not another entire PG
    # cluster. Require explicit reuse once the retained acceptance cluster exists.
    retained = ROOT / ".tmp/trial-pg-import-4yycc3ou"
    if not args.resume_work and (retained / "data/PG_VERSION").is_file():
        parser.error("Reuse the retained stopped cluster with --resume-work .tmp/trial-pg-import-4yycc3ou; do not allocate another cluster.")
    if args.resume_work:
        work = args.resume_work.resolve()
        if work.parent != (ROOT / ".tmp").resolve() or not work.name.startswith("trial-pg-import-") or not (work / "data/PG_VERSION").is_file() or (work / "data/postmaster.pid").exists():
            raise ValueError("Resume requires a stopped, task-owned acceptance cluster.")
    else:
        work = Path(tempfile.mkdtemp(prefix="trial-pg-import-", dir=ROOT / ".tmp"))
    attempt = Path(tempfile.mkdtemp(prefix="proof-", dir=work))
    # Keep this supplementary acceptance data compressed on a small Windows
    # workspace. Logical database bytes remain unchanged and recoverable.
    subprocess.run(["compact.exe", "/C", str(work)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True, creationflags=subprocess.CREATE_NO_WINDOW)
    pg = ROOT / ".tools/postgresql17/pgsql/bin"
    began = time.monotonic()
    started = False
    def run(*command):
        with (work / "lifecycle.log").open("ab") as output:
            subprocess.run(command, check=True, timeout=90, stdout=output, stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        if not args.resume_work:
            run(str(pg / "initdb.exe"), "-D", str(work / "data"), "-A", "trust", "-U", "acceptance", "--no-locale", "-E", "UTF8", "--wal-segsize=1")
        started = True
        run(str(pg / "pg_ctl.exe"), "-D", str(work / "data"), "-l", str(work / "server.log"), "-w", "start", "-o", f"-h 127.0.0.1 -p {port} -c timezone=UTC -c max_wal_size=128MB -c min_wal_size=16MB -c log_min_messages=panic -c max_locks_per_transaction=2048")
        url = f"postgresql://acceptance@127.0.0.1:{port}/postgres"
        import metahub_shared.db.connection as connection_module
        original_raise = connection_module._raise_postgres_error
        def schema_diagnostic(error):
            (work / "schema-error.json").write_text(json.dumps({"sqlstate": error.sqlstate, "primary": error.diag.message_primary}))
            original_raise(error)
        connection_module._raise_postgres_error = schema_diagnostic
        result = apply_migrations(url)
        connection_module._raise_postgres_error = original_raise
        print(json.dumps({"phase": "native_schema", "applied_count": len(result.applied), "skipped_count": len(result.skipped), "elapsed": time.monotonic() - began}), flush=True)
        secret = attempt / "target.env"
        secret.write_text("METAHUB_DATABASE_URL=" + url + "\n")
        spec = importlib.util.spec_from_file_location("trial_import", ROOT / "scripts/deploy/sqlite-to-postgres.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        sys.argv = ["sqlite-to-postgres.py", "--source", str(args.source), "--snapshot", str(attempt / "source.sqlite3"), "--target-secret-file", str(secret), "--report", str(attempt / "import-report.json"), "--verify-current-schema"]
        module.main()
        restart = apply_migrations(url)
        if restart.applied:
            raise ValueError("Completed import did not leave an idempotent migration state.")
        with psycopg.connect(url) as connection:
            size = connection.execute("SELECT pg_database_size(current_database())").fetchone()[0]
        (attempt / "result.json").write_text(json.dumps({"status": "verified", "platform": "Windows native PostgreSQL (supplementary)", "database_bytes": size, "elapsed_seconds": time.monotonic()-began, "migration_rerun_skipped_count": len(restart.skipped)}, indent=2))
        print(json.dumps({"status": "verified", "report": str(attempt / "import-report.json"), "database_bytes": size}), flush=True)
    except Exception as error:
        import traceback
        (attempt / "private-diagnostic.txt").write_text(traceback.format_exc(), encoding="utf-8")
        print(json.dumps({"status": "failed", "type": type(error).__name__, "code": getattr(error, "code", None), "work": str(work), "attempt": str(attempt)}), flush=True)
        raise SystemExit(1) from None
    finally:
        close_database_pools()
        if started:
            run(str(pg / "pg_ctl.exe"), "-D", str(work / "data"), "-w", "stop", "-m", "fast")


if __name__ == "__main__":
    main()
