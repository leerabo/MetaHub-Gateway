"""Actual auth and business routes, in-process; not an isolated deployment attestation."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import secrets
import shutil
import socket
from contextlib import AsyncExitStack, asynccontextmanager, suppress
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from metahub_assurance_runner.bootstrap import HttpFixtureBootstrap
from metahub_assurance_runner.lease_channel import (
    GuestLease,
    LeaseBinding,
    StreamLeaseTransport,
    SupervisorLeaseSession,
)
from metahub_assurance_runner.probes import (
    IMPLEMENTED_SCENARIOS,
    RESOURCE_SCENARIOS,
    FixedProbeRunner,
)
from metahub_assurance_runner.resources import LocalResourceInspector
from metahub_control.files.routes import router as files_router
from metahub_control.identity.delivery import pop_test_identity_challenge
from metahub_control.identity.routes import router as identity_router
from metahub_control.projects.routes import router as projects_router
from metahub_control.workspace_access.routes import router as access_router
from metahub_control.workspace_navigation.routes import router as navigation_router
from metahub_control.workspaces.routes import router as workspaces_router
from metahub_shared.db import apply_migrations, apply_seed_data
from metahub_shared.runtime.fastapi_app import create_service_app

ORIGIN = "https://assurance.test"


@asynccontextmanager
async def _lease_pipe(authority, deadline):
    left, right = socket.socketpair()
    server_reader, server_writer = await asyncio.open_connection(sock=left)
    guest_reader, guest_writer = await asyncio.open_connection(sock=right)
    binding = LeaseBinding(
        request_sha256=hashlib.sha256(b"in-process real-auth integration only").hexdigest(),
        environment_id="aenv_" + uuid4().hex,
        assessment_generation=1,
        deadline_at=deadline,
        channel_nonce=secrets.token_hex(32),
    )
    session = SupervisorLeaseSession(
        StreamLeaseTransport(server_reader, server_writer),
        binding,
        authority,
    )
    task = asyncio.create_task(session.serve())
    lease = None
    try:
        lease = await GuestLease.accept(StreamLeaseTransport(guest_reader, guest_writer))
        yield lease, task
    finally:
        if lease is not None:
            lease.close()
        session.close()
        if not task.done():
            task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        for writer in (server_writer, guest_writer):
            writer.close()
            await writer.wait_closed()


@pytest.fixture(scope="module")
def migrated_baseline(tmp_path_factory):
    supplied = os.environ.get("METAHUB_TEST_MIGRATED_BASELINE")
    if supplied:
        baseline = Path(supplied).resolve(strict=True)
        assert baseline.is_file()
        current = tmp_path_factory.mktemp("prd137-current-probe-base") / "baseline.sqlite3"
        shutil.copy2(baseline, current)
        apply_migrations(f"sqlite:///{current.as_posix()}")
        return current
    baseline = tmp_path_factory.mktemp("prd137-probe-base") / "baseline.sqlite3"
    url = f"sqlite:///{baseline.as_posix()}"
    apply_migrations(url)
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("METAHUB_SEED_ADMIN_PASSWORD", secrets.token_urlsafe(32))
        apply_seed_data(url)
    return baseline


@pytest.fixture
def runtime(migrated_baseline, tmp_path, monkeypatch):
    database = tmp_path / "probe.sqlite3"
    shutil.copy2(migrated_baseline, database)
    url = f"sqlite:///{database.as_posix()}"
    assert not apply_migrations(url).applied
    settings = {
        "METAHUB_ENV": "test",
        "METAHUB_DATABASE_URL": url,
        "METAHUB_ALLOWED_ORIGINS": ORIGIN,
        "METAHUB_IDENTITY_CHALLENGE_DELIVERY_MODE": "test_capture",
        "METAHUB_WORKSPACE_ALLOWED_REGIONS": "cn-north-1",
        "METAHUB_CONTROL_BACKGROUND_SCHEDULERS_ENABLED": "false",
        "METAHUB_FILE_STORAGE_ROOT": str(tmp_path / "file-storage"),
    }
    for key in (
        "AUTH_TOKEN",
        "IDENTITY_SESSION",
        "IDENTITY_CHALLENGE",
        "IDENTITY_PII_ENCRYPTION",
        "WORKSPACE_SESSION",
        "FILE_ENCRYPTION",
    ):
        settings[f"METAHUB_{key}_SECRET"] = secrets.token_urlsafe(48)
    for key, value in settings.items():
        monkeypatch.setenv(key, value)
    app = create_service_app(
        "control-plane-api",
        "PRD-137 real-auth probe integration",
        extra_cors_allow_headers=("If-Match", "X-CSRF-Token"),
        cors_allow_credentials=True,
    )
    for router in (
        identity_router,
        access_router,
        workspaces_router,
        projects_router,
        files_router,
        navigation_router,
    ):
        app.include_router(router)
    assert not app.dependency_overrides
    return app


@pytest.mark.anyio
async def test_fixed_probes_with_registered_accounts_and_real_authorization(
    runtime,
    caplog,
    record_property,
):
    sensitive = []
    lease_checks = 0

    async def test_lease_guard():
        nonlocal lease_checks
        lease_checks += 1
        return True

    async def mailbox(intent_id, email):
        challenge = pop_test_identity_challenge("registration", intent_id)
        assert challenge is not None and challenge.target == email
        sensitive.extend((email, challenge.code))
        return challenge.code

    async def capture_test_secrets(request):
        if request.headers.get("content-type") == "application/json":
            payload = json.loads(request.content)
            sensitive.extend(
                payload[key] for key in ("password", "invitation_token") if key in payload
            )

    async with AsyncExitStack() as stack:
        clients = [
            await stack.enter_async_context(
                AsyncClient(
                    transport=ASGITransport(app=runtime),
                    base_url=ORIGIN,
                    follow_redirects=False,
                    trust_env=False,
                    event_hooks={"request": [capture_test_secrets]},
                )
            )
            for _ in range(4)
        ]
        deadline = datetime.now(UTC) + timedelta(minutes=5)
        lease, session_task = await stack.enter_async_context(
            _lease_pipe(test_lease_guard, deadline)
        )
        bootstrap = HttpFixtureBootstrap(
            *clients,
            origin=ORIGIN,
            environment_id=lease.binding.environment_id,
            mailbox=mailbox,
            lease_guard=lease.guard,
            deadline_at=deadline,
        )
        fixtures = await bootstrap.run()
        assert len(bootstrap.observations) == 13
        assert all(
            re.fullmatch(r"[0-9a-f]{64}", item.identity_sha256) for item in bootstrap.observations
        )
        resources = LocalResourceInspector(
            Path(runtime.state.config.file_storage_root).parent / "probe.sqlite3",
            Path(runtime.state.config.file_storage_root),
            runtime.state.config.file_encryption_secret,
        )
        runner = FixedProbeRunner(fixtures, lease.guard, deadline, resources=resources)
        results = [await runner.run(code) for code in (*RESOURCE_SCENARIOS, *IMPLEMENTED_SCENARIOS)]
        serialized = json.dumps([asdict(result) for result in results], default=str)
        record_property("probe_results", serialized)
        assert lease_checks > 100
        sensitive.extend(cookie.value for client in clients for cookie in client.cookies.jar)
        public_output = serialized + repr(bootstrap) + repr(bootstrap.observations) + caplog.text
        assert all(value not in public_output for value in sensitive)
        assert all(result.outcome == "passed" for result in results), [
            (
                result.scenario_code,
                result.outcome,
                result.reason_code,
                [check.code for check in result.checks if not check.satisfied],
            )
            for result in results
        ]
        report_sha = hashlib.sha256(serialized.encode()).hexdigest()
        assert await lease.finish(report_sha)
        announced = await session_task
        assert announced.report_sha256 == report_sha
        assert announced.checks_permitted > 100
