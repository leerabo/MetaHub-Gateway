"""Private real Control/static Console instance for the browser race journey."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import importlib.util
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import traceback
from contextlib import AsyncExitStack
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import httpx
import uvicorn
from cryptography import x509
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from metahub_assurance_runner.bootstrap import HttpFixtureBootstrap
from metahub_assurance_runner.guest_runtime import _initialize
from starlette.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parents[3]


def _windows_child_job():
    if os.name != "nt":
        return None
    name = "_metahub_prd137_browser_process_owner"
    module = sys.modules.get(name)
    if module is None:
        specification = importlib.util.spec_from_file_location(
            name, ROOT / "scripts/acceptance/prd077-browser-harness.py"
        )
        if specification is None or specification.loader is None:
            raise RuntimeError("browser_process_owner_missing")
        module = importlib.util.module_from_spec(specification)
        sys.modules[name] = module
        specification.loader.exec_module(module)
    return module.WindowsKillOnCloseJob()


async def main():
    baseline, scratch, node, browser = (Path(value).resolve(strict=True) for value in sys.argv[1:5])
    width = int(sys.argv[5])
    screenshot = Path(sys.argv[6]).resolve()
    journey = sys.argv[7] if len(sys.argv) > 7 else "identity"
    scripts = {
        "identity": "prd137LateResponseJourney.mjs",
        "console-entry": "prd137ConsoleEntryJourney.mjs",
    }
    if width not in (1440, 390) or screenshot.parent != scratch.parent or journey not in scripts:
        raise ValueError("browser_test_boundary")
    before = hashlib.sha256(baseline.read_bytes()).hexdigest()
    original = Path.cwd()
    child = None
    child_job = None
    server = None
    server_task = None
    runtime_stage = "initialize"
    report = {
        "schema": "metahub.browser-late-response.v1",
        "status": "failed",
        "checks": [],
        "process_exited_successfully": False,
    }
    private = tempfile.TemporaryDirectory(prefix="prd137-browser-", dir=scratch)
    directory = Path(private.name)
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        listener.bind(("127.0.0.1", 0))
        listener.listen(16)
        listener.setblocking(False)
        origin = f"https://127.0.0.1:{listener.getsockname()[1]}"
        app, cert, key = await asyncio.to_thread(
            _initialize,
            baseline,
            directory,
            origin,
            ROOT / "infra/migrations",
        )
        if journey == "console-entry":
            from metahub_control.console_experience import install_console_experience_scheduler

            install_console_experience_scheduler(app)
        app.mount("/", StaticFiles(directory=ROOT / "console-web", html=True))
        server = uvicorn.Server(
            uvicorn.Config(
                app,
                ssl_certfile=str(cert),
                ssl_keyfile=str(key),
                access_log=False,
                log_config=None,
                proxy_headers=False,
                lifespan="on",
                timeout_graceful_shutdown=2,
            )
        )
        server_task = asyncio.create_task(server.serve(sockets=[listener]))
        async with asyncio.timeout(45):
            while not server.started:
                if server_task.done():
                    await server_task
                    raise RuntimeError("browser_api_stopped")
                await asyncio.sleep(0.05)

        async def allowed():
            return not server_task.done()

        async def mailbox(intent_id, email):
            from metahub_control.identity.delivery import pop_test_identity_challenge

            challenge = pop_test_identity_challenge("registration", intent_id)
            if challenge is None or challenge.target != email:
                raise RuntimeError("browser_mailbox_missing")
            return challenge.code

        async with AsyncExitStack() as stack:
            clients = [
                await stack.enter_async_context(
                    httpx.AsyncClient(
                        base_url=origin,
                        verify=str(cert),
                        trust_env=False,
                        follow_redirects=False,
                    )
                )
                for _ in range(4)
            ]
            bootstrap = HttpFixtureBootstrap(
                *clients,
                origin=origin,
                environment_id="aenv_" + uuid4().hex,
                mailbox=mailbox,
                lease_guard=allowed,
                deadline_at=datetime.now(UTC) + timedelta(minutes=5),
            )
            runtime_stage = "bootstrap"
            fixtures = await bootstrap.run()
            projection_scheduler_running = False
            if journey == "console-entry":
                async with asyncio.timeout(60):
                    while True:
                        projection = await clients[0].get("/workspace/overview")
                        if projection.status_code == 200:
                            break
                        if projection.status_code != 503:
                            raise RuntimeError("console_projection_authentication_failed")
                        await asyncio.sleep(0.25)
                projection_scheduler_running = any(
                    task.get_name() == "console-experience-scheduler" and not task.done()
                    for task in asyncio.all_tasks()
                )
                if not projection_scheduler_running:
                    raise RuntimeError("console_projection_scheduler_stopped")
            environment = {
                key: os.environ[key] for key in ("SYSTEMROOT", "WINDIR") if key in os.environ
            }
            environment.update(
                {"TEMP": str(directory), "TMP": str(directory), "NODE_EXTRA_CA_CERTS": str(cert)}
            )
            public_key = x509.load_pem_x509_certificate(cert.read_bytes()).public_key()
            spki = base64.b64encode(
                hashlib.sha256(
                    public_key.public_bytes(
                        Encoding.DER,
                        PublicFormat.SubjectPublicKeyInfo,
                    )
                ).digest()
            ).decode("ascii")
            runtime_stage = "browser_process"
            child_job = _windows_child_job()
            child = subprocess.Popen(
                [str(node), str(ROOT / "tests/product_qa/web" / scripts[journey])],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=directory,
                env=environment,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            # The script waits for stdin EOF before launch, so no browser can precede assignment.
            if child_job is not None:
                child_job.assign(child)
            payload = json.dumps(
                {
                    "origin": origin,
                    "spki": spki,
                    "browser": str(browser),
                    "email": fixtures.source_account.email,
                    "password": fixtures.source_account.password,
                    "workspace_id": fixtures.source_workspace_id,
                    "viewport": {"width": width, "height": 900 if width == 1440 else 844},
                    "screenshot": str(screenshot),
                }
            ).encode("utf-8")
            runtime_stage = "browser_journey"
            browser_timed_out = False
            try:
                stdout, stderr = await asyncio.to_thread(child.communicate, payload, 180)
            except subprocess.TimeoutExpired:
                browser_timed_out = True
                if child_job is not None:
                    await asyncio.to_thread(child_job.terminate_and_wait, 10)
                elif child.poll() is None:
                    child.kill()
                stdout, stderr = await asyncio.to_thread(child.communicate, None, 10)
            if len(stdout) > 32_768 or len(stderr) > 32_768:
                raise RuntimeError("browser_report_unbounded")
            private_values = (fixtures.source_account.email, fixtures.source_account.password)
            if any(value.encode() in stdout + stderr for value in private_values):
                raise RuntimeError("browser_report_private_material")
            try:
                report = json.loads(stdout)
            except (UnicodeError, ValueError):
                report = {
                    "schema": "metahub.browser-late-response.v1",
                    "status": "failed",
                    "stage": "child_report_missing",
                    "child_exit_code": child.returncode,
                    "stderr_categories": [
                        marker
                        for marker in (
                            "ERR_MODULE_NOT_FOUND",
                            "SyntaxError",
                            "ReferenceError",
                            "TypeError",
                            "TimeoutError",
                            "browserType.launch",
                            "Target page, context or browser has been closed",
                            "ENOENT",
                            "EACCES",
                        )
                        if marker.encode("ascii") in stderr
                    ],
                    "script_lines": [
                        int(line)
                        for line in re.findall(
                            rb"prd137(?:ConsoleEntry|LateResponse)Journey\.mjs:(\d+):\d+",
                            stderr,
                        )[:8]
                    ],
                    "checks": [],
                }
            if report.get("schema") != "metahub.browser-late-response.v1":
                raise RuntimeError("browser_report_schema")
            report["browser_timed_out"] = browser_timed_out
            if browser_timed_out:
                report["status"] = "failed"
            report["process_exited_successfully"] = child.returncode == 0
            report["stderr_empty"] = not stderr
            report["console_projection_scheduler_running"] = projection_scheduler_running
    except Exception as error:
        report["status"] = "failed"
        report["runtime_stage"] = runtime_stage
        report["runtime_error_type"] = type(error).__name__
    finally:
        try:
            if child_job is not None:
                report["child_processes_remaining"] = await asyncio.to_thread(
                    child_job.terminate_and_wait, 10
                )
            elif child is not None and child.poll() is None:
                child.terminate()
            if child is not None:
                await asyncio.to_thread(child.wait, 10)
        except Exception as error:
            report["status"] = "failed"
            report["child_cleanup_error"] = type(error).__name__
        finally:
            if child_job is not None:
                try:
                    child_job.close()
                except Exception as error:
                    report["status"] = "failed"
                    report["child_job_close_error"] = type(error).__name__
            if child is not None:
                for stream in (child.stdin, child.stdout, child.stderr):
                    if stream is not None:
                        stream.close()
        if server is not None:
            server.should_exit = True
        try:
            if server_task is not None:
                await asyncio.wait_for(server_task, 10)
        except Exception as error:
            report["status"] = "failed"
            report["server_cleanup_error"] = type(error).__name__
        finally:
            listener.close()
            from metahub_shared.db.connection import (
                _close_idle_database_connections,
                close_database_pools,
            )

            _close_idle_database_connections()
            close_database_pools()
            os.chdir(original)
            try:
                private.cleanup()
            except OSError as cleanup_error:
                report["status"] = "failed"
                report["private_cleanup_error"] = type(cleanup_error).__name__
    report["private_directory_removed"] = not directory.exists()
    report["baseline_unchanged"] = hashlib.sha256(baseline.read_bytes()).hexdigest() == before
    print(json.dumps(report))
    return 0 if report["status"] == "passed" and report["process_exited_successfully"] else 1


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except Exception as error:
        print(type(error).__name__, file=sys.stderr)
        for frame in traceback.extract_tb(error.__traceback__):
            print(f"{Path(frame.filename).name}:{frame.lineno}:{frame.name}", file=sys.stderr)
        raise SystemExit(1) from None
