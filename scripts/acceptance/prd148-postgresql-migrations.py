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
from metahub_shared.db import rollback_last_migration  # noqa: E402
from metahub_shared.db.errors import DbError  # noqa: E402

source_snapshot = runpy.run_path(
    str(ROOT / "scripts/quality/product_qa_evidence.py")
)["source_snapshot"]


def applied_version(connection) -> str:
    return connection.execute(
        "SELECT version FROM schema_migrations WHERE status='applied' "
        "ORDER BY version DESC LIMIT 1"
    ).fetchone()[0]


def assert_denied(connection, statement, parameters=(), *, sqlstate="P0001"):
    try:
        connection.execute(statement, parameters)
    except psycopg.Error as exc:
        assert exc.sqlstate == sqlstate
    else:
        raise AssertionError("Protected database write was accepted")


def assert_down_denied(url, *, version):
    try:
        rollback_last_migration(url, expected_version=version)
    except DbError as exc:
        assert exc.code == "db.migration_failed"
    else:
        raise AssertionError("Protected facts must block down")


def verify_review_lifecycle(connection, url, *, account, operator, stamp, report):
    """Synthetic schema facts in the disposable cluster, never real approvals."""
    tenant = "11111111-1111-4111-8111-111111111111"
    connection.execute(
        "INSERT INTO tenants VALUES(%s,'ga-native-review','GA native review',"
        "'internal_only','active',%s,%s)", (tenant, stamp, stamp),
    )
    # The production tenant initializer owns the Workspace/binding projection.
    workspace = connection.execute(
        "SELECT workspace_id FROM workspace_tenant_bindings WHERE tenant_id=%s", (tenant,),
    ).fetchone()[0]
    candidate = "lvc_" + "a" * 32
    launch = "gal_" + "a" * 32
    connection.execute(
        "INSERT INTO launch_candidates VALUES("
        "%s,%s,%s,%s,%s,%s,%s,%s,1,'collecting',%s,1,1,%s,%s,NULL)",
        (candidate, *("f" * 64 for _ in range(7)), operator, stamp, stamp),
    )
    connection.execute(
        "INSERT INTO ga_launches VALUES(%s,%s,%s,%s,'launched',1,1,%s,%s,%s,%s)",
        (launch, candidate, workspace, tenant, stamp, stamp, stamp, stamp),
    )
    review_sql = "INSERT INTO post_ga_reviews VALUES(%s,%s,%s,%s,%s,%s,%s)"
    first = ("pgr_" + "a" * 32, launch, "b" * 64, "c" * 64,
             "ga_launched", operator, stamp)
    second = ("pgr_" + "b" * 32, launch, "d" * 64, "e" * 64,
              "ga_rolled_back", operator, stamp)
    connection.execute(review_sql, first)
    rows = connection.execute("SELECT * FROM post_ga_reviews ORDER BY post_ga_review_id").fetchall()
    assert rollback_last_migration(url, expected_version="0163").rolled_back == "0163"
    assert applied_version(connection) == "0162"
    assert connection.execute("SELECT * FROM post_ga_reviews").fetchall() == rows
    assert_denied(connection, review_sql, second, sqlstate="23505")
    report["checks"].append("single_review_down_preserves_fact_and_old_uniqueness")
    assert apply_migrations(url).applied == ["0163"]
    connection.execute(review_sql, second)
    rows = connection.execute("SELECT * FROM post_ga_reviews ORDER BY post_ga_review_id").fetchall()
    permissions = connection.execute(
        "SELECT * FROM platform_launch_review_role_permissions ORDER BY role_code"
    ).fetchall()
    history = connection.execute(
        "SELECT * FROM schema_migrations WHERE version='0163'"
    ).fetchall()
    assert_down_denied(url, version="0163")
    assert applied_version(connection) == "0163"
    assert connection.execute(
        "SELECT * FROM post_ga_reviews ORDER BY post_ga_review_id"
    ).fetchall() == rows
    assert connection.execute(
        "SELECT * FROM platform_launch_review_role_permissions ORDER BY role_code"
    ).fetchall() == permissions
    assert connection.execute(
        "SELECT * FROM schema_migrations WHERE version='0163'"
    ).fetchall() == history
    assert apply_migrations(url).applied == []
    report["checks"].append("two_conclusions_down_rejected_with_facts_permissions_history_intact")
    assert_denied(connection, review_sql, second, sqlstate="23505")
    for statement in (
        "DELETE FROM post_ga_reviews",
        "UPDATE post_ga_reviews SET summary_sha256=action_items_sha256",
    ):
        assert_denied(connection, statement)
    for statement in (
        "DELETE FROM platform_launch_review_role_permissions",
        "UPDATE platform_launch_review_role_permissions SET introduced_revision=1",
        "INSERT INTO platform_launch_review_role_permissions "
        "VALUES('platform_owner','platform.launch.review','PRD-148',1)",
    ):
        assert_denied(connection, statement)
    report["checks"].append("review_facts_and_review_permissions_immutable")


def main() -> int:
    work = Path(tempfile.mkdtemp(prefix="prd148-native-", dir=ROOT / "outputs")).resolve()
    assert work.parent == (ROOT / "outputs").resolve()
    report = {"status": "failed", "migration": "0163", "checks": [], "cluster_stopped": False,
              "source_start": source_snapshot()}
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
        assert "0163" in applied
        report["checks"].append("full_native_migration_chain")
        assert apply_migrations(url).applied == []
        report["checks"].append("migration_replay_noop")
        close_database_pools()
        print("Native 0163 chain ready; validating both migration owners and facts.", flush=True)
        with psycopg.connect(url, autocommit=True) as connection:
            gates = connection.execute(
                "SELECT * FROM ga_gate_policies ORDER BY ga_gate_id"
            ).fetchall()
            assert len(gates) == 20
            report["checks"].append("twenty_policies")
            assert rollback_last_migration(url, expected_version="0163").rolled_back == "0163"
            assert applied_version(connection) == "0162"
            assert connection.execute(
                "SELECT to_regclass('platform_launch_review_role_permissions')"
            ).fetchone()[0] is None
            assert connection.execute(
                "SELECT * FROM ga_gate_policies ORDER BY ga_gate_id"
            ).fetchall() == gates
            report["checks"].append("review_down_preserves_prior_migration_objects")
            assert rollback_last_migration(url, expected_version="0162").rolled_back == "0162"
            assert (
                connection.execute("SELECT to_regclass('ga_gate_policies')").fetchone()[0] is None
            )
            report["checks"].append("empty_down")
            assert applied_version(connection) == "0161"
            assert apply_migrations(url).applied == ["0162", "0163"]
            assert (
                connection.execute("SELECT * FROM ga_gate_policies ORDER BY ga_gate_id").fetchall()
                == gates
            )
            report["checks"].append("policy_preserving_up_after_down")
            assert_denied(connection, "DELETE FROM ga_gate_policies")
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
            assert rollback_last_migration(url, expected_version="0163").rolled_back == "0163"
            assert_down_denied(url, version="0162")
            assert applied_version(connection) == "0162"
            assert connection.execute("SELECT count(*) FROM ga_action_commands").fetchone()[0] == 1
            assert (
                connection.execute("SELECT * FROM ga_gate_policies ORDER BY ga_gate_id").fetchall()
                == gates
            )
            report["checks"].append("command_only_down_rejected_without_data_loss")
            assert apply_migrations(url).applied == ["0163"]
            verify_review_lifecycle(
                connection, url, account=account, operator=operator, stamp=stamp, report=report,
            )
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
            report["source_finish"] = source_snapshot()
            if report["source_finish"] != report["source_start"]:
                report["status"] = "failed"
                report["reason"] = "source_changed_during_acceptance"
            report["elapsed_seconds"] = round(time.monotonic() - began, 2)
            (work / "report.json").write_text(
                json.dumps(report, indent=2) + "\n", encoding="utf-8", newline="\n"
            )
            print(json.dumps(report), flush=True)
            print("Evidence:", work.relative_to(ROOT), flush=True)
    return 0 if report["status"] == "passed" and report["cluster_stopped"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
