"""Run real signup/organization/consumer browser flows without touching the trial instance."""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [
    str(ROOT / part)
    for part in (
        "shared-sdk/python",
        "control-plane-api/src",
        "gateway-data-plane/src",
    )
]


def serve(app):
    import uvicorn

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=listener.getsockname()[1],
            lifespan="off",
            access_log=False,
            log_level="critical",
            proxy_headers=False,
        )
    )
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 30
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert server.started, "isolated service did not start"
    return server, thread, listener


def main():
    node = os.environ.get("METAHUB_S1_NODE") or shutil.which("node")
    baseline = Path(os.environ["METAHUB_TEST_METER_BASELINE"]).resolve(strict=True)
    assert baseline.is_relative_to(ROOT / ".tmp")
    with baseline.open("rb") as source:
        assert hashlib.file_digest(source, "sha256").hexdigest() == (
            "062ae8e3a8024030efb712a5120793483203b86d552fcfd86a46e9bf804f4c2a"
        )
    assert node and shutil.disk_usage(ROOT).free > baseline.stat().st_size + 60 * 1024 * 1024
    work = Path(tempfile.mkdtemp(prefix="s1-browser-", dir=ROOT / ".tmp"))
    artifacts = ROOT / ".artifacts" / work.name
    artifacts.mkdir()
    database = work / "acceptance.sqlite3"
    shutil.copy2(baseline, database)
    # The subprocess never reads the user's .env or inherits MetaHub service credentials.
    for key in tuple(os.environ):
        if key.startswith("METAHUB_"):
            os.environ.pop(key)
    os.chdir(work)
    settings = {
        "METAHUB_ENV": "dev",
        "METAHUB_DATABASE_URL": "sqlite:///" + database.as_posix(),
        "METAHUB_SEED_ADMIN_PASSWORD": secrets.token_urlsafe(32),
        "METAHUB_IDENTITY_CHALLENGE_DELIVERY_MODE": "disabled",
        "METAHUB_CONTROL_BACKGROUND_SCHEDULERS_ENABLED": "false",
    }
    for key in (
        "AUTH_TOKEN_SECRET",
        "IDENTITY_SESSION_SECRET",
        "IDENTITY_CHALLENGE_SECRET",
        "IDENTITY_PII_ENCRYPTION_SECRET",
        "WORKSPACE_SESSION_SECRET",
        "PLATFORM_SESSION_SECRET",
        "SUPPORT_SESSION_SECRET",
        "IDENTITY_MFA_ENCRYPTION_SECRET",
    ):
        settings["METAHUB_" + key] = secrets.token_urlsafe(48)
    os.environ.update(settings)
    logging.disable(logging.CRITICAL)
    from metahub_shared.db import apply_migrations, apply_seed_data, connect_database
    from metahub_shared.db.connection import _close_idle_database_connections, close_database_pools

    apply_migrations(settings["METAHUB_DATABASE_URL"], migrations_dir=ROOT / "infra/migrations")
    apply_seed_data(
        settings["METAHUB_DATABASE_URL"],
        seed_file=ROOT / "infra/seeds/default_seed.json",
    )
    from fastapi.staticfiles import StaticFiles
    from metahub_control.app import app as control
    from metahub_control.identity import registration_policy
    from metahub_gateway.app import app as gateway

    class CaptchaRandom:
        @staticmethod
        def choice(values):
            return values[0]

        randbelow = staticmethod(secrets.randbelow)

    # Only this module's challenge generator is deterministic; tokens remain random.
    registration_policy.secrets = CaptchaRandom
    control.mount("/", StaticFiles(directory=ROOT / "console-web", html=True))
    services = []
    result = {"status": "failed", "provider_calls": 0, "started_at": datetime.now(UTC).isoformat()}
    try:
        services.append(serve(control))
        services.append(serve(gateway))
        origin = "http://s1.localhost:" + str(services[0][2].getsockname()[1])
        control.state.config = control.state.config.model_copy(update={"allowed_origins": [origin]})
        with closing(connect_database(settings["METAHUB_DATABASE_URL"])) as db:
            initial = {
                table: db.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]
                for table in ("global_accounts", "users", "workspace_memberships")
            }
        env = dict(
            os.environ,
            S1_ORIGIN=origin,
            S1_ARTIFACTS=str(artifacts),
            S1_GATEWAY_ORIGIN="http://127.0.0.1:" + str(services[1][2].getsockname()[1]),
        )
        browser_tmp = work / "browser-tmp"
        browser_tmp.mkdir()
        env.update(TEMP=str(browser_tmp), TMP=str(browser_tmp))
        completed = subprocess.run(
            [node, str(ROOT / "scripts/acceptance/s1-organization-browser.mjs")],
            env=env,
            cwd=work,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=240,
        )
        browser_report = json.loads((artifacts / "browser.json").read_text(encoding="utf-8"))
        result["browser"] = browser_report
        assert completed.returncode == 0, "browser_flow_failed"
        with closing(connect_database(settings["METAHUB_DATABASE_URL"])) as db:
            delta = {
                table: db.execute("SELECT COUNT(*) FROM " + table).fetchone()[0] - count
                for table, count in initial.items()
            }
            assert delta == {"global_accounts": 2, "users": 2, "workspace_memberships": 2}, delta
            result["identity_count_delta"] = delta
            assert db.execute("SELECT COUNT(*) FROM workspace_consumers").fetchone()[0] == 4
            assert db.execute("SELECT COUNT(*) FROM virtual_key_consumers").fetchone()[0] == 4
            assert (
                db.execute(
                    "SELECT COUNT(*) FROM account_verifications v JOIN global_accounts a "
                    "ON a.account_id=v.account_id WHERE a.email_normalized LIKE 's1-browser-%'"
                ).fetchone()[0]
                == 0
            )
            assert db.execute("SELECT COUNT(*) FROM platform_operators").fetchone()[0] == 0
            assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        result["status"] = "passed"
    except Exception as error:
        result["failure_type"] = type(error).__name__
    finally:
        for server, thread, listener in reversed(services):
            server.should_exit = True
            thread.join(timeout=15)
            assert not thread.is_alive(), "isolated server shutdown failed"
            listener.close()
        close_database_pools()
        _close_idle_database_connections()
        if result["status"] != "passed":
            with closing(connect_database(settings["METAHUB_DATABASE_URL"])) as db:
                db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            archive = database.with_suffix(".sqlite3.gz")
            with database.open("rb") as source, gzip.open(archive, "xb") as output:
                shutil.copyfileobj(source, output)
            with database.open("rb") as raw, gzip.open(archive, "rb") as compressed:
                assert (
                    hashlib.file_digest(raw, "sha256").digest()
                    == hashlib.file_digest(compressed, "sha256").digest()
                )
            result["failure_database_compressed"] = True
        for suffix in ("", "-wal", "-shm"):
            database.with_name(database.name + suffix).unlink(missing_ok=True)
        result["isolated_database_retained"] = database.exists()
        result["worker_lifespan"] = "disabled"
        result["frontend_source"] = "console-web (unmodified HTTP source bytes)"
        (artifacts / "acceptance.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(
            json.dumps(
                {
                    "status": result["status"],
                    "artifacts": str(artifacts),
                    "provider_calls": 0,
                    "services_stopped": True,
                }
            )
        )
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
