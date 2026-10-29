"""Run real signup/organization/consumer browser flows without touching the trial instance."""

from __future__ import annotations

import faulthandler
import gzip
import hashlib
import asyncio
import json
import logging
import os
import secrets
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[2]
SOURCE = Path(os.getenv("METAHUB_S1_SOURCE_ROOT", str(ROOT))).resolve(strict=True)
if SOURCE != ROOT and not SOURCE.is_relative_to(ROOT / ".tmp"):
    raise ValueError("S1 source must be the workspace or its isolated export")
if os.getenv("METAHUB_S1_PRODUCTION", "false") not in {"true", "false"}:
    raise ValueError("S1 production mode must be true or false")
PRODUCTION = os.getenv("METAHUB_S1_PRODUCTION", "false") == "true"
FLOW = os.getenv("METAHUB_S1_FLOW", "organization")
if FLOW not in {"organization", "finance"}:
    raise ValueError("S1 flow must be organization or finance")
FRONTEND = SOURCE / "console-web" / "dist" if PRODUCTION else SOURCE / "console-web"
if os.getenv("METAHUB_S1_FRONTEND_ROOT"):
    FRONTEND = Path(os.environ["METAHUB_S1_FRONTEND_ROOT"]).resolve(strict=True)
    if not PRODUCTION or not FRONTEND.is_relative_to(ROOT / ".tmp") or FRONTEND.name != "dist":
        raise ValueError("Candidate frontend must be an isolated production build")
sys.path[:0] = [
    str(SOURCE / part)
    for part in (
        "shared-sdk/python",
        "control-plane-api/src",
        "gateway-data-plane/src",
    )
]


def serve(app):
    import uvicorn

    # Windows ProactorEventLoop can crash in asyncio's self-pipe writer when
    # Uvicorn is hosted from a background thread. The isolated browser runner
    # does not need Proactor-only APIs, so use the selector loop for stable
    # evidence collection while leaving product runtime configuration untouched.
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=listener.getsockname()[1],
            lifespan="off",
            loop="none",
            access_log=False,
            log_level="critical",
            proxy_headers=False,
            timeout_graceful_shutdown=5,
        )
    )
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 30
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert server.started, "isolated service did not start"
    return server, thread, listener


class _PortHandle:
    """Expose a subprocess port through the existing service bookkeeping API."""

    def __init__(self, port: int):
        self._address = ("127.0.0.1", port)

    def getsockname(self):
        return self._address

    def close(self):
        return None


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def remove_isolated_database_files(database: Path) -> None:
    """Remove the isolated SQLite files after Windows releases service handles."""
    for suffix in ("", "-wal", "-shm"):
        candidate = database.with_name(database.name + suffix)
        deadline = time.monotonic() + 5
        while True:
            try:
                candidate.unlink(missing_ok=True)
                break
            except PermissionError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.1)


def serve_subprocess(module_name: str):
    """Run a secondary ASGI service outside the diagnostic control process."""
    port = _free_port()
    source_roots = [
        SOURCE / "shared-sdk" / "python",
        SOURCE / "control-plane-api" / "src",
        SOURCE / "gateway-data-plane" / "src",
    ]
    environment = os.environ.copy()
    environment["PYTHONPATH"] = os.pathsep.join(str(path) for path in source_roots)
    code = (
        "import asyncio,sys,uvicorn; "
        "asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy()) "
        "if sys.platform == 'win32' else None; "
        f"uvicorn.run('{module_name}:app', host='127.0.0.1', port={port}, "
        "lifespan='off', loop='none', access_log=False, proxy_headers=False)"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", code],
        cwd=SOURCE,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"{module_name}_service_exited")
        try:
            with urlopen(f"http://127.0.0.1:{port}/health", timeout=1) as response:  # noqa: S310
                if response.status == 200:
                    return process, None, _PortHandle(port)
        except (OSError, URLError):
            time.sleep(0.1)
    process.terminate()
    process.wait(timeout=5)
    raise RuntimeError(f"{module_name}_service_timeout")


def serve_control_subprocess(frontend: Path):
    """Run the browser control plane outside the parent diagnostic process."""
    port = _free_port()
    origin = f"http://s1.localhost:{port}"
    source_roots = [
        SOURCE / "shared-sdk" / "python",
        SOURCE / "control-plane-api" / "src",
        SOURCE / "gateway-data-plane" / "src",
    ]
    environment = os.environ.copy()
    environment["PYTHONPATH"] = os.pathsep.join(str(path) for path in source_roots)
    environment["METAHUB_ALLOWED_ORIGINS"] = origin
    process = subprocess.Popen(
        [sys.executable, str(SOURCE / "scripts/acceptance/s1-control-service.py"),
         "--port", str(port), "--frontend", str(frontend)],
        cwd=SOURCE,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("control_service_exited")
        try:
            with urlopen(f"http://127.0.0.1:{port}/health", timeout=1) as response:  # noqa: S310
                if response.status == 200:
                    return process, None, _PortHandle(port), origin
        except (OSError, URLError):
            time.sleep(0.1)
    process.terminate()
    process.wait(timeout=5)
    raise RuntimeError("control_service_timeout")


def create_browser_app(control, usage_fixture=None):
    """Serve public assets before API routing, as the trial frontend does."""
    from starlette.applications import Starlette
    from starlette.routing import Mount, Route
    from starlette.staticfiles import StaticFiles

    surface = json.loads(
        (SOURCE / "console-web/production-surface-manifest.json").read_text(encoding="utf-8")
    )
    entrypoints = surface["html_entrypoints"]
    assert all(Path(name).name == name and name.endswith(".html") for name in entrypoints)
    static = StaticFiles(directory=FRONTEND, html=True)
    return Starlette(routes=[
        Mount("/src", app=StaticFiles(directory=FRONTEND / "src")),
        Route("/", endpoint=static),
        *(Route("/" + name, endpoint=static) for name in entrypoints),
        *([Route("/__acceptance/usage", endpoint=usage_fixture, methods=["POST"])]
          if usage_fixture is not None else []),
        Mount("/", app=control),
    ])


def navigation_diagnostics(operation, failures):
    """Record handled database failures without retaining SQL, arguments or credentials."""
    def call(*args, **kwargs):
        previous = sys.gettrace()

        def trace(frame, event, value):
            if frame.f_code.co_filename != operation.__code__.co_filename:
                return None
            if event == "exception" and isinstance(value[1], sqlite3.Error):
                if len(failures) < 20:
                    failures.append({"function": frame.f_code.co_name,
                                     "type": type(value[1]).__name__,
                                     "sqlite_code": getattr(value[1], "sqlite_errorname", None)})
            return trace

        sys.settrace(trace)
        try:
            return operation(*args, **kwargs)
        finally:
            sys.settrace(previous)

    return call


def main():
    node = os.environ.get("METAHUB_S1_NODE") or shutil.which("node")
    baseline = Path(os.environ["METAHUB_TEST_METER_BASELINE"]).resolve(strict=True)
    assert baseline.is_relative_to(ROOT / ".tmp")
    with baseline.open("rb") as source:
        assert hashlib.file_digest(source, "sha256").hexdigest() == (
            "d1634e8b3b3cc6fbd96e8ca6f0d7c5f79007ddb090b49384ade6c8123279fb03"
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
    from metahub_shared.db.connection import (
        _SqliteConnection,
        _close_idle_database_connections,
        close_database_pools,
    )

    apply_migrations(settings["METAHUB_DATABASE_URL"], migrations_dir=SOURCE / "infra/migrations")
    apply_seed_data(
        settings["METAHUB_DATABASE_URL"],
        seed_file=SOURCE / "infra/seeds/default_seed.json",
    )
    slow_sql = []
    sql_events_path = artifacts / "sql-events.jsonl"
    original_sql_execute = _SqliteConnection.execute

    def timed_sql_execute(connection, sql, parameters=()):
        started = time.monotonic()
        if any(token in str(sql).upper() for token in ("BEGIN", "INSERT", "UPDATE", "DELETE", "PRAGMA JOURNAL_MODE")):
            with sql_events_path.open("a", encoding="utf-8") as events:
                events.write(json.dumps({
                    "phase": "start",
                    "statement": str(sql).split()[0:3],
                    "thread": threading.get_ident(),
                }) + "\n")
        
        try:
            return original_sql_execute(connection, sql, parameters)
        finally:
            elapsed = time.monotonic() - started
            if elapsed >= 0.5 and len(slow_sql) < 100:
                slow_sql.append({
                    "elapsed_ms": round(elapsed * 1000, 1),
                    "statement": str(sql).split()[0:3],
                    "thread": threading.get_ident(),
                })
                with sql_events_path.open("a", encoding="utf-8") as events:
                    events.write(json.dumps({
                        "phase": "slow-end",
                        "elapsed_ms": round(elapsed * 1000, 1),
                        "statement": str(sql).split()[0:3],
                        "thread": threading.get_ident(),
                    }) + "\n")

    _SqliteConnection.execute = timed_sql_execute
    from metahub_control.app import app as control
    from metahub_control.identity import registration_policy
    from metahub_control.workspace_navigation import routes as navigation_routes
    from metahub_gateway.app import app as gateway

    navigation_failures = []
    for operation in ("get_workspace_navigation", "record_workspace_page_visit"):
        setattr(navigation_routes, operation, navigation_diagnostics(
            getattr(navigation_routes, operation), navigation_failures,
        ))

    for module in ("metahub_control.app", "metahub_gateway.app", "metahub_shared.db"):
        assert Path(sys.modules[module].__file__).resolve().is_relative_to(SOURCE)

    class CaptchaRandom:
        @staticmethod
        def choice(values):
            return values[0]

        randbelow = staticmethod(secrets.randbelow)

    # Only this module's challenge generator is deterministic; tokens remain random.
    registration_policy.secrets = CaptchaRandom
    services = []
    fixture_token = secrets.token_urlsafe(48)
    usage_fixture = None
    if FLOW == "finance":
        from s2_usage_fixture import create_usage_fixture

        usage_fixture = create_usage_fixture(database, SOURCE, fixture_token)
    result = {"status": "failed", "provider_calls": 0, "flow": FLOW,
              "started_at": datetime.now(UTC).isoformat()}
    try:
        if FLOW == "organization":
            control_service = serve_control_subprocess(FRONTEND)
            services.append(control_service[:3])
            origin = control_service[3]
        else:
            services.append(serve(create_browser_app(control, usage_fixture)))
            origin = "http://s1.localhost:" + str(services[0][2].getsockname()[1])
        # Keep Gateway outside the diagnostic control process. Both services
        # use the same isolated database, while independent process ownership
        # avoids Windows async loop crashes contaminating browser evidence.
        services.append(serve_subprocess("metahub_gateway.app"))
        if FLOW != "organization":
            control.state.config = control.state.config.model_copy(update={"allowed_origins": [origin]})
        if PRODUCTION:
            import httpx

            manifest = json.loads((FRONTEND / "production-assets.json").read_text(encoding="utf-8"))
            with httpx.Client(
                base_url="http://127.0.0.1:" + str(services[0][2].getsockname()[1]),
                trust_env=False,
                timeout=10,
            ) as client:
                for asset in manifest["assets"]:
                    assert (FRONTEND / asset["path"]).resolve().is_relative_to(FRONTEND)
                    response = client.get("/" + asset["path"])
                    assert response.status_code == 200
                    assert hashlib.sha256(response.content).hexdigest() == asset["sha256"]
            result["verified_production_assets"] = len(manifest["assets"])
            result["asset_set_sha256"] = manifest["asset_set_sha256"]
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
            S1_FLOW=FLOW,
            S1_FIXTURE_TOKEN=fixture_token,
            S1_PYTHON=sys.executable,
        )
        browser_tmp = work / "browser-tmp"
        browser_tmp.mkdir()
        env.update(TEMP=str(browser_tmp), TMP=str(browser_tmp))
        with (artifacts / "server-stacks.txt").open("w", encoding="utf-8") as stacks:
            faulthandler.enable(file=stacks)
            # Capture service-thread ownership while the browser flow is live. This is
            # diagnostic evidence for the open page-visit contention only; it never
            # changes request deadlines or turns a failed flow into a pass.
            faulthandler.dump_traceback_later(5, repeat=True, file=stacks)
            try:
                completed = subprocess.run(
                    [node, str(ROOT / "scripts/acceptance/s1-organization-browser.mjs")],
                    env=env,
                    cwd=work,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    # Account entry, Provider instantiation, SDK downloads and navigation
                    # run for both viewports. Per-browser-operation deadlines remain 12s;
                    # this only prevents the outer process budget from ending a healthy
                    # second viewport before it can report its own terminal result.
                    timeout=480 if FLOW == "finance" else 720,
                )
            except subprocess.TimeoutExpired:
                faulthandler.dump_traceback(file=stacks)
                raise
            finally:
                faulthandler.cancel_dump_traceback_later()
                faulthandler.disable()
        (artifacts / "browser-stdout.txt").write_text(completed.stdout or "", encoding="utf-8")
        (artifacts / "browser-stderr.txt").write_text(completed.stderr or "", encoding="utf-8")
        browser_report = json.loads((artifacts / "browser.json").read_text(encoding="utf-8"))
        result["browser"] = browser_report
        assert completed.returncode == 0, "browser_flow_failed"
        with closing(connect_database(settings["METAHUB_DATABASE_URL"])) as db:
            delta = {
                table: db.execute("SELECT COUNT(*) FROM " + table).fetchone()[0] - count
                for table, count in initial.items()
            }
            organizations = 3 if FLOW == "finance" else 2
            accounts = organizations if FLOW == "finance" else organizations * 2
            assert delta == {table: accounts for table in initial}, delta
            result["identity_count_delta"] = delta
            for table in ("workspace_consumers", "virtual_key_consumers"):
                count = db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                assert count == organizations * 2
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
            if thread is None:
                server.terminate()
                try:
                    server.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.wait(timeout=5)
                assert server.poll() is not None, "isolated subprocess shutdown failed"
            else:
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
        remove_isolated_database_files(database)
        result["isolated_database_retained"] = database.exists()
        result["worker_lifespan"] = "disabled"
        result["source_root"] = str(SOURCE)
        result["frontend_source"] = str(FRONTEND)
        result["production_build"] = PRODUCTION
        result["static_dispatch"] = "public_assets_before_control"
        result["navigation_database_failures"] = navigation_failures
        result["slow_sql"] = slow_sql
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
