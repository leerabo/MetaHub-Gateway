"""Run isolated real-browser product journeys without touching developer data."""

from __future__ import annotations

import json
import os
import secrets
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[2]
for source_root in reversed(
    (
        ROOT / "shared-sdk" / "python",
        ROOT / "gateway-data-plane" / "src",
        ROOT / "control-plane-api" / "src",
    )
):
    sys.path.insert(0, str(source_root))

from metahub_shared.db import apply_migrations, apply_seed_data, connect_database  # noqa: E402
from metahub_shared.db.security import hash_password  # noqa: E402

PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"
NODE = Path(r"C:\Program Files\nodejs\node.exe")
BROWSER_SCRIPT = ROOT / "tests" / "product_qa" / "web" / "realBrowserJourneys.mjs"
ARTIFACT_ROOT = Path(
    os.environ.get("METAHUB_QA_ARTIFACT_ROOT", ROOT / ".artifacts" / "product-qa")
).resolve()
TEMP_ROOT = ROOT / ".tmp" / "product-qa-browser"
BROWSER_JOURNEY_TIMEOUT_SECONDS = 180


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _wait_http(url: str, process: subprocess.Popen[bytes]) -> None:
    deadline = time.monotonic() + 35
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("product_qa_service_exited")
        try:
            with urlopen(url, timeout=1) as response:  # noqa: S310 - fixed loopback URL
                if response.status == 200:
                    return
        except (OSError, URLError):
            pass
        time.sleep(0.15)
    raise RuntimeError("product_qa_service_timeout")


def _stop(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=8)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _prepare_database(database_url: str, passwords: dict[str, str]) -> dict[str, object]:
    previous_seed_password = os.environ.get("METAHUB_SEED_ADMIN_PASSWORD")
    os.environ["METAHUB_SEED_ADMIN_PASSWORD"] = passwords["admin"]
    try:
        migration = apply_migrations(database_url)
        seed = apply_seed_data(database_url)
    finally:
        if previous_seed_password is None:
            os.environ.pop("METAHUB_SEED_ADMIN_PASSWORD", None)
        else:
            os.environ["METAHUB_SEED_ADMIN_PASSWORD"] = previous_seed_password

    connection = connect_database(database_url)
    try:
        journal_mode = str(connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]).lower()
        if journal_mode != "wal":
            raise RuntimeError("product_qa_database_journal_invalid")
        for role, email, display_name in (
            ("developer", "qa-developer@example.invalid", "QA Developer"),
            ("viewer", "qa-viewer@example.invalid", "QA Viewer"),
        ):
            connection.execute(
                """
                INSERT INTO users(
                  user_id,tenant_id,email,display_name,password_hash,role,status,
                  created_at,updated_at
                ) VALUES(?,?,?,?,?,?,'active',datetime('now'),datetime('now'))
                """,
                (
                    f"usr_product_qa_{role}",
                    seed.tenant_id,
                    email,
                    display_name,
                    hash_password(passwords[role]),
                    role,
                ),
            )
        admin_user = connection.execute(
            "SELECT user_id FROM users WHERE tenant_id=? AND email='admin@metahub.local'",
            (seed.tenant_id,),
        ).fetchone()
        if admin_user is None:
            raise RuntimeError("product_qa_admin_missing")
        locale_update = connection.execute(
            "UPDATE user_preferences SET locale='en-US' WHERE tenant_id=? AND user_id=?",
            (seed.tenant_id, admin_user["user_id"]),
        )
        if locale_update.rowcount == 0:
            connection.execute(
                """
                INSERT INTO user_preferences(
                  preference_id,tenant_id,user_id,active_navigation_item_id,
                  sidebar_collapsed,locale,created_at,updated_at
                ) VALUES(?,?,?,'overview',0,'en-US',datetime('now'),datetime('now'))
                """,
                (f"upr_product_qa_{secrets.token_hex(8)}", seed.tenant_id, admin_user["user_id"]),
            )
        connection.commit()
        migration_count = connection.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE status='applied'"
        ).fetchone()[0]
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        foreign_key_violations = len(connection.execute("PRAGMA foreign_key_check").fetchall())
    finally:
        connection.close()
    return {
        "applied_migrations": int(migration_count),
        "latest_migration": migration.applied[-1] if migration.applied else None,
        "integrity_check": integrity,
        "foreign_key_violations": foreign_key_violations,
        "journal_mode": journal_mode,
    }


def main() -> int:
    run_id = f"{time.strftime('%Y%m%d-%H%M%S', time.gmtime())}-{secrets.token_hex(4)}"
    run_root = TEMP_ROOT / run_id
    run_root.mkdir(parents=True, exist_ok=False)
    ARTIFACT_ROOT.mkdir(parents=True, exist_ok=True)
    browser_name = os.environ.get("METAHUB_QA_BROWSER", "chromium").lower()
    scale_name = os.environ.get("METAHUB_QA_SCALE", "1").replace(".", "_")
    evidence_path = ARTIFACT_ROOT / f"browser-real-{browser_name}-scale-{scale_name}.json"
    database_url = f"sqlite:///{(run_root / 'runtime.db').as_posix()}"
    passwords = {role: secrets.token_urlsafe(32) for role in ("admin", "developer", "viewer")}
    database_facts = _prepare_database(database_url, passwords)
    if database_facts["integrity_check"] != "ok" or database_facts["foreign_key_violations"] != 0:
        raise RuntimeError("product_qa_database_invalid")

    gateway_port, control_port, console_port = _free_port(), _free_port(), _free_port()
    source_roots = (
        ROOT / "shared-sdk" / "python",
        ROOT / "gateway-data-plane" / "src",
        ROOT / "control-plane-api" / "src",
    )
    environment = os.environ.copy()
    environment.update(
        {
            "PYTHONPATH": os.pathsep.join(str(path) for path in source_roots),
            "METAHUB_ENV": "dev",
            "METAHUB_DATABASE_URL": database_url,
            "METAHUB_SEED_ADMIN_PASSWORD": passwords["admin"],
            "METAHUB_AUTH_TOKEN_SECRET": secrets.token_urlsafe(48),
            "METAHUB_ALLOWED_ORIGINS": f"http://127.0.0.1:{console_port}",
            "METAHUB_CONTROL_API_BASE_URL": f"http://127.0.0.1:{control_port}",
            "METAHUB_GATEWAY_API_BASE_URL": f"http://127.0.0.1:{gateway_port}",
            "METAHUB_CONTROL_BACKGROUND_SCHEDULERS_ENABLED": "false",
            "METAHUB_PROVIDER_HEALTH_ENABLED": "false",
            "METAHUB_POLICY_SNAPSHOT_SYNC_ENABLED": "false",
            "METAHUB_PROVIDER_ERROR_MAP_SYNC_ENABLED": "false",
            "METAHUB_CONSOLE_PORT": str(console_port),
            "METAHUB_QA_CONTROL_PORT": str(control_port),
            "METAHUB_QA_GATEWAY_PORT": str(gateway_port),
            "METAHUB_QA_CONSOLE_PORT": str(console_port),
            "METAHUB_QA_ADMIN_EMAIL": "admin@metahub.local",
            "METAHUB_QA_ADMIN_PASSWORD": passwords["admin"],
            "METAHUB_QA_DEVELOPER_EMAIL": "qa-developer@example.invalid",
            "METAHUB_QA_DEVELOPER_PASSWORD": passwords["developer"],
            "METAHUB_QA_VIEWER_EMAIL": "qa-viewer@example.invalid",
            "METAHUB_QA_VIEWER_PASSWORD": passwords["viewer"],
        }
    )
    control_log = (run_root / "control.log").open("wb")
    gateway_log = (run_root / "gateway.log").open("wb")
    console_log = (run_root / "console.log").open("wb")
    control = subprocess.Popen(
        [
            str(PYTHON),
            "-m",
            "uvicorn",
            "metahub_control.app:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(control_port),
            "--no-access-log",
        ],
        cwd=ROOT,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=control_log,
        stderr=subprocess.STDOUT,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    gateway = subprocess.Popen(
        [
            str(PYTHON),
            "-m",
            "uvicorn",
            "metahub_gateway.app:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(gateway_port),
            "--no-access-log",
        ],
        cwd=ROOT,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=gateway_log,
        stderr=subprocess.STDOUT,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    console = subprocess.Popen(
        [str(NODE), "scripts/dev-server.mjs"],
        cwd=ROOT / "console-web",
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=console_log,
        stderr=subprocess.STDOUT,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        _wait_http(f"http://127.0.0.1:{control_port}/health", control)
        _wait_http(f"http://127.0.0.1:{gateway_port}/health", gateway)
        _wait_http(f"http://127.0.0.1:{console_port}/", console)
        browser_run = subprocess.run(
            [str(NODE), str(BROWSER_SCRIPT)],
            cwd=ROOT,
            env=environment,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
            timeout=BROWSER_JOURNEY_TIMEOUT_SECONDS,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if browser_run.returncode != 0:
            diagnostic = browser_run.stderr.decode("utf-8", errors="replace")
            for private_value in (
                *passwords.values(),
                environment["METAHUB_QA_ADMIN_EMAIL"],
                environment["METAHUB_QA_DEVELOPER_EMAIL"],
                environment["METAHUB_QA_VIEWER_EMAIL"],
            ):
                diagnostic = diagnostic.replace(private_value, "[redacted]")
            (run_root / "browser.err").write_text(diagnostic, encoding="utf-8")
            raise RuntimeError("product_qa_browser_journey_failed")
        browser_facts = json.loads(browser_run.stdout.decode("utf-8"))
        evidence = {
            "schema": "metahub.product-qa.browser.v1",
            "database": database_facts,
            "browser": browser_facts,
            "all_108_prds_complete": False,
        }
        evidence_path.write_text(
            json.dumps(evidence, ensure_ascii=True, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        combined_logs = control_log.name
        del combined_logs
        print(json.dumps({"journeys": len(browser_facts["journeys"]), "passed": True}))
        return 0
    finally:
        _stop(console)
        _stop(gateway)
        _stop(control)
        console_log.close()
        gateway_log.close()
        control_log.close()
        for secret in passwords.values():
            for log_path in (
                run_root / "control.log",
                run_root / "gateway.log",
                run_root / "console.log",
            ):
                if log_path.exists() and secret.encode("utf-8") in log_path.read_bytes():
                    raise RuntimeError("product_qa_secret_in_service_log")


if __name__ == "__main__":
    raise SystemExit(main())
