"""Private real Control/static Console instance for the browser race journey."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import socket
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


async def main():
    baseline, scratch, node, browser = (Path(value).resolve(strict=True) for value in sys.argv[1:5])
    width = int(sys.argv[5])
    screenshot = Path(sys.argv[6]).resolve()
    if width not in (1440, 390) or screenshot.parent != scratch.parent:
        raise ValueError("browser_test_boundary")
    before = hashlib.sha256(baseline.read_bytes()).hexdigest()
    original = Path.cwd()
    child = None
    server = None
    server_task = None
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
            fixtures = await bootstrap.run()
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
            child = await asyncio.create_subprocess_exec(
                str(node),
                str(ROOT / "tests/product_qa/web/prd137LateResponseJourney.mjs"),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=directory,
                env=environment,
            )
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
            stdout, stderr = await asyncio.wait_for(child.communicate(payload), 300)
            if len(stdout) > 32_768 or len(stderr) > 32_768:
                raise RuntimeError("browser_report_unbounded")
            private_values = (fixtures.source_account.email, fixtures.source_account.password)
            if any(value.encode() in stdout + stderr for value in private_values):
                raise RuntimeError("browser_report_private_material")
            report = json.loads(stdout)
            if report.get("schema") != "metahub.browser-late-response.v1":
                raise RuntimeError("browser_report_schema")
            report["process_exited_successfully"] = child.returncode == 0
            report["stderr_empty"] = not stderr
    finally:
        if child is not None and child.returncode is None:
            child.terminate()
            await child.wait()
        if server is not None:
            server.should_exit = True
        try:
            if server_task is not None:
                await asyncio.wait_for(server_task, 10)
        finally:
            listener.close()
            from metahub_shared.db.connection import (
                _close_idle_database_connections,
                close_database_pools,
            )

            _close_idle_database_connections()
            close_database_pools()
            os.chdir(original)
            private.cleanup()
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
