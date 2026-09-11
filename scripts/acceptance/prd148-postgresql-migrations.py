"""Verify GA up/down/replay against a disposable native PostgreSQL cluster."""

from __future__ import annotations

import json
import runpy
import socket
import tempfile
import time
import traceback
from pathlib import Path

# Share the existing invisible Windows process and isolated-cluster tooling.
ROOT = Path(__file__).resolve().parents[2]
runtime = runpy.run_path(str(ROOT / "scripts/acceptance/s3-postgresql-runtime.py"))
run = runtime["run"]
PG = runtime["PG"]
psycopg = runtime["psycopg"]
apply_migrations = runtime["apply_migrations"]
close_database_pools = runtime["close_database_pools"]


def main() -> int:
    work = Path(tempfile.mkdtemp(prefix="prd148-native-", dir=ROOT / "outputs")).resolve()
    assert work.parent == (ROOT / "outputs").resolve()
    report = {"status": "failed", "migration": "0162", "checks": [], "cluster_stopped": False}
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
            "ga_test",
            "--no-locale",
            "-E",
            "UTF8",
            "--wal-segsize=1",
        )
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
        url = f"postgresql://ga_test@127.0.0.1:{port}/postgres"
        applied = apply_migrations(url).applied
        assert "0162" in applied
        report["checks"].append("full_native_migration_chain")
        assert apply_migrations(url).applied == []
        report["checks"].append("migration_replay_noop")
        close_database_pools()
        print("Native 0162 chain ready; validating rollback and fact protection.", flush=True)
        up = (ROOT / "infra/migrations/postgresql/0162_saas_ga_launch.sql").read_text(
            encoding="utf-8"
        )
        down = (ROOT / "infra/migrations/postgresql/0162_saas_ga_launch.down.sql").read_text(
            encoding="utf-8"
        )
        with psycopg.connect(url, autocommit=True) as connection:
            gates = connection.execute(
                "SELECT * FROM ga_gate_policies ORDER BY ga_gate_id"
            ).fetchall()
            assert len(gates) == 20
            report["checks"].append("twenty_policies")
            connection.execute(down)
            assert (
                connection.execute("SELECT to_regclass('ga_gate_policies')").fetchone()[0] is None
            )
            report["checks"].append("empty_down")
            connection.execute(up)
            assert (
                connection.execute("SELECT * FROM ga_gate_policies ORDER BY ga_gate_id").fetchall()
                == gates
            )
            report["checks"].append("policy_preserving_up_after_down")
            try:
                connection.execute("DELETE FROM ga_gate_policies")
            except psycopg.Error as exc:
                assert exc.sqlstate == "P0001"
            else:
                raise AssertionError("Gate deletion must be rejected")
            report["checks"].append("immutable_policies")
            # A single action receipt is already a protected audit fact, even without a launch.
            account = "gac_" + "a" * 32
            operator = "pto_" + "a" * 32
            stamp = "2026-09-11T00:00:00.000Z"
            connection.execute(
                """INSERT INTO global_accounts(
                account_id,email_normalized,phone_e164,display_name,status,login_enabled,
                credential_revision,session_revision,mfa_state,failed_login_count,
                locked_until,last_login_at,created_at,updated_at)
                VALUES(%s,%s,NULL,'GA native test','active',0,1,1,
                'not_enrolled',0,NULL,NULL,%s,%s)""",
                (account, "ga-native@example.invalid", stamp, stamp),
            )
            connection.execute(
                """INSERT INTO platform_operators(
                operator_id,account_id,status,revision,created_by_operator_id,created_at,updated_at)
                VALUES(%s,%s,'active',1,NULL,%s,%s)""",
                (operator, account, stamp, stamp),
            )
            connection.execute(
                "INSERT INTO ga_action_commands VALUES(%s,%s,%s,%s,%s,%s,1,%s)",
                (
                    "gac_" + "b" * 32,
                    operator,
                    "ga.evidence",
                    "c" * 64,
                    "d" * 64,
                    "gae_" + "e" * 32,
                    stamp,
                ),
            )
            try:
                connection.execute(down)
            except psycopg.Error as exc:
                assert exc.sqlstate == "P0001" and "ga_facts_present" in str(exc)
                connection.execute("ROLLBACK")
            else:
                raise AssertionError("Audit facts must block down")
            assert connection.execute("SELECT count(*) FROM ga_action_commands").fetchone()[0] == 1
            assert (
                connection.execute("SELECT * FROM ga_gate_policies ORDER BY ga_gate_id").fetchall()
                == gates
            )
            report["checks"].append("command_only_down_rejected_without_data_loss")
        report["status"] = "passed"
    except Exception as exc:
        report["error_type"] = type(exc).__name__
        report["error_code"] = getattr(exc, "sqlstate", getattr(exc, "code", None))
        report["error_line"] = traceback.extract_tb(exc.__traceback__)[-1].lineno
        # Only exception type is persisted; connection strings and arbitrary SQL data are omitted.
        print("Native validation failed:", type(exc).__name__, flush=True)
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
            (work / "report.json").write_text(
                json.dumps(report, indent=2) + "\n", encoding="utf-8", newline="\n"
            )
            print(json.dumps(report), flush=True)
            print("Evidence:", work.relative_to(ROOT), flush=True)
    return 0 if report["status"] == "passed" and report["cluster_stopped"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
