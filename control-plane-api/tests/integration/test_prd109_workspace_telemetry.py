"""Real Workspace authentication, middleware queue and background persistence."""

import asyncio
import hashlib
import hmac
import sqlite3
from collections import Counter
from contextlib import AsyncExitStack, closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from metahub_assurance_runner.bootstrap import HttpFixtureBootstrap
from metahub_assurance_runner.probes import WORKSPACE_SESSION
from metahub_control.identity.delivery import pop_test_identity_challenge
from metahub_shared.observability import install_telemetry_runtime
from test_prd137_http_probes import ORIGIN, migrated_baseline, runtime  # noqa: F401
from test_prd147_workspace_files import _headers


@pytest.fixture
async def telemetry_subjects(runtime):  # noqa: F811
    if not hasattr(runtime.state, "telemetry_runtime"):
        install_telemetry_runtime(runtime, service_code="control")

    async def allowed():
        return True

    async def mailbox(intent_id, email):
        challenge = pop_test_identity_challenge("registration", intent_id)
        assert challenge is not None and challenge.target == email
        return challenge.code

    async with AsyncExitStack() as stack:
        await stack.enter_async_context(runtime.router.lifespan_context(runtime))
        clients = [
            await stack.enter_async_context(
                AsyncClient(
                    transport=ASGITransport(runtime),
                    base_url=ORIGIN,
                    trust_env=False,
                )
            )
            for _ in range(4)
        ]
        subjects = await HttpFixtureBootstrap(
            *clients,
            origin=ORIGIN,
            environment_id="aenv_" + uuid4().hex,
            mailbox=mailbox,
            lease_guard=allowed,
            deadline_at=datetime.now(UTC) + timedelta(minutes=3),
        ).run()
        async with asyncio.timeout(5):
            buffer = runtime.state.telemetry_runtime
            while buffer.snapshot().persisted != buffer.snapshot().accepted:
                await asyncio.sleep(0.05)
        database = Path(runtime.state.config.file_storage_root).parent / "probe.sqlite3"
        yield runtime, database, subjects


@pytest.mark.anyio
async def test_workspace_http_buckets_bind_verified_owner_and_reject_revoked_labels(
    telemetry_subjects,
):
    app, database, fixtures = telemetry_subjects
    secret = app.state.config.platform_session_secret
    buffer = app.state.telemetry_runtime
    before = buffer.snapshot()
    forged = {
        "X-Tenant-ID": fixtures.target_tenant_id,
        "X-Telemetry-Operation": "private-operation-canary",
    }
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as connection:
        watermark = connection.execute(
            "SELECT source_watermark FROM telemetry_ingest_head"
        ).fetchone()[0]
    async with asyncio.timeout(30):
        assert not app.dependency_overrides
        assert (await fixtures.source.get("/projects", headers=forged)).status_code == 200
        assert (await fixtures.target.get("/projects")).status_code == 200
        assert (
            await fixtures.anonymous.patch(
                f"/projects/{fixtures.target_project_id}",
                headers={**_headers(fixtures.source, attack=True), **forged, "Origin": ORIGIN},
                json={"name": "unauthorized-change"},
            )
        ).status_code == 404
        assert (await fixtures.anonymous.get("/projects", headers=forged)).status_code == 401
        assert (
            await fixtures.anonymous.get(
                "/private-operation-canary",
                headers=forged,
            )
        ).status_code == 404
        old_cookie = fixtures.source.cookies.get(WORKSPACE_SESSION)
        assert (
            await fixtures.source.post(
                "/identity/workspace-switches",
                json={"workspace_id": fixtures.source_workspace_id},
                headers={**_headers(fixtures.source, global_session=True), "Origin": ORIGIN},
            )
        ).status_code == 200
        assert fixtures.source.cookies.get(WORKSPACE_SESSION) != old_cookie
        assert (
            await fixtures.anonymous.get(
                "/projects",
                headers={**forged, "Cookie": f"{WORKSPACE_SESSION}={old_cookie}"},
            )
        ).status_code == 401
        assert (await fixtures.source.get("/projects")).status_code == 200
        async with asyncio.timeout(5):
            while buffer.snapshot().persisted != buffer.snapshot().accepted:
                await asyncio.sleep(0.05)
        snapshot = buffer.snapshot()
        assert snapshot.accepted - before.accepted == snapshot.persisted - before.persisted == 8
        assert snapshot.queued == snapshot.dropped == snapshot.backend_failures == 0
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as connection:
        rows = connection.execute(
            """SELECT tenant_pseudonym,status_class,operation_code,region_code,total_count
               FROM sli_time_buckets WHERE source_watermark>?""",
            (watermark,),
        ).fetchall()
    counts = Counter()
    for owner, status, operation, region, total in rows:
        assert operation == "other" and region == "global"
        counts[(owner, status)] += total

    def pseudonym(tenant_id):
        return hmac.new(
            secret.encode(), f"prd109/telemetry-tenant/v1\0{tenant_id}".encode(), hashlib.sha256
        ).hexdigest()

    assert counts == {
        (pseudonym(fixtures.source_tenant_id), "2xx"): 2,
        (pseudonym(fixtures.source_tenant_id), "4xx"): 1,
        (pseudonym(fixtures.target_tenant_id), "2xx"): 1,
        ("none", "4xx"): 3,
        ("none", "2xx"): 1,
    }
    assert all(
        value not in repr(rows)
        for value in (
            fixtures.source_tenant_id,
            fixtures.target_tenant_id,
            secret,
            old_cookie,
            "private-operation-canary",
        )
    )
