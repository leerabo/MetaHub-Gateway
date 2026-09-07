"""Actual Workspace file authentication and transaction revocation, not G7 signoff."""

import asyncio
import secrets
import shutil
import sqlite3
import threading
from contextlib import AsyncExitStack, closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from metahub_assurance_runner.bootstrap import HttpFixtureBootstrap
from metahub_assurance_runner.probes import GLOBAL_CSRF, WORKSPACE_CSRF, WORKSPACE_SESSION
from metahub_control.files import routes as file_routes
from metahub_control.files import service as file_service
from metahub_control.identity.delivery import pop_test_identity_challenge
from metahub_control.identity.routes import router as identity_router
from metahub_control.projects.routes import router as projects_router
from metahub_control.workspace_access.routes import router as access_router
from metahub_control.workspace_access.service import authenticate_workspace_session_as_auth_context
from metahub_control.workspaces.routes import router as workspaces_router
from metahub_shared.runtime.fastapi_app import create_service_app
from test_prd137_http_probes import migrated_baseline  # noqa: F401

ORIGIN = "https://workspace-files.test"


def _headers(client, *, global_session=False, attack=False):
    headers = {
        "Origin": ORIGIN,
        "Sec-Fetch-Site": "same-site",
        "X-CSRF-Token": client.cookies.get(GLOBAL_CSRF if global_session else WORKSPACE_CSRF),
    }
    if attack:
        headers["Cookie"] = client.build_request("GET", "/").headers["cookie"]
    return headers


@pytest.fixture
async def file_subjects(migrated_baseline, tmp_path, monkeypatch):  # noqa: F811
    database = tmp_path / "files.sqlite3"
    shutil.copyfile(migrated_baseline, database)
    settings = {
        "METAHUB_ENV": "test",
        "METAHUB_DATABASE_URL": f"sqlite:///{database.as_posix()}",
        "METAHUB_ALLOWED_ORIGINS": ORIGIN,
        "METAHUB_IDENTITY_CHALLENGE_DELIVERY_MODE": "test_capture",
        "METAHUB_WORKSPACE_ALLOWED_REGIONS": "cn-north-1",
        "METAHUB_CONTROL_BACKGROUND_SCHEDULERS_ENABLED": "false",
        "METAHUB_FILE_STORAGE_ROOT": str(tmp_path / "file-storage"),
    }
    for domain in (
        "AUTH_TOKEN",
        "IDENTITY_SESSION",
        "IDENTITY_CHALLENGE",
        "IDENTITY_PII_ENCRYPTION",
        "WORKSPACE_SESSION",
        "FILE_ENCRYPTION",
    ):
        settings[f"METAHUB_{domain}_SECRET"] = secrets.token_urlsafe(48)
    for name, value in settings.items():
        monkeypatch.setenv(name, value)
    app = create_service_app("control-plane-api", "Workspace files integration")
    for router in (
        identity_router,
        access_router,
        workspaces_router,
        projects_router,
        file_routes.router,
    ):
        app.include_router(router)
    assert not app.dependency_overrides

    async def allowed():
        return True

    async def mailbox(intent_id, email):
        challenge = pop_test_identity_challenge("registration", intent_id)
        assert challenge is not None and challenge.target == email
        return challenge.code

    async with AsyncExitStack() as stack:
        clients = [
            await stack.enter_async_context(
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app),
                    base_url=ORIGIN,
                    trust_env=False,
                )
            )
            for _ in range(4)
        ]
        fixtures = await HttpFixtureBootstrap(
            *clients,
            origin=ORIGIN,
            environment_id="aenv_" + uuid4().hex,
            mailbox=mailbox,
            lease_guard=allowed,
            deadline_at=datetime.now(UTC) + timedelta(minutes=3),
        ).run()
        yield app, database, fixtures


@pytest.mark.anyio
async def test_workspace_file_guard_precedes_body_and_keeps_tenant_scope(
    file_subjects, monkeypatch
):
    _app, database, f = file_subjects
    original_reader = file_routes.read_single_file_part

    async def unreadable(*args, **kwargs):
        pytest.fail("denied request reached multipart parser")

    monkeypatch.setattr(file_routes, "read_single_file_part", unreadable)
    source_headers = _headers(f.source, attack=True)
    for headers, expected in (
        ({}, 401),
        ({**source_headers, "X-CSRF-Token": "invalid"}, 403),
        ({**source_headers, "Origin": "https://foreign.test"}, 403),
        ({**source_headers, "Authorization": "Bearer invalid"}, 400),
    ):
        response = await f.anonymous.post("/files", content=b"not multipart", headers=headers)
        assert response.status_code == expected, response.json()["code"]
    monkeypatch.setattr(file_routes, "read_single_file_part", original_reader)
    uploaded = await f.source.post(
        "/files",
        files={"file": ("same-name.txt", b"source isolated content", "text/plain")},
        headers=_headers(f.source),
    )
    assert uploaded.status_code == 201, uploaded.json().get("code")
    file_id = uploaded.json()["file"]["file_id"]
    assert uploaded.json()["file"]["status"] == "ready"
    own = await f.source.get(f"/files/{file_id}")
    assert own.status_code == 200 and own.json()["file"] == uploaded.json()["file"]
    foreign = await f.anonymous.get(f"/files/{file_id}", headers=_headers(f.target, attack=True))
    assert foreign.status_code == 404 and foreign.json()["code"] == "file.not_found"
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM uploaded_files").fetchone()[0] == 1


@pytest.mark.anyio
async def test_workspace_file_terminal_rechecks_real_downgrade_and_stale_context(
    file_subjects,
    monkeypatch,
):
    app, database, f = file_subjects
    stale = authenticate_workspace_session_as_auth_context(
        app.state.config,
        f.member.cookies.get(WORKSPACE_SESSION),
    )
    entered, release = threading.Event(), threading.Event()
    original_extract = file_service.extract_file_text

    def paused_extract(*args, **kwargs):
        entered.set()
        if not release.wait(20):
            raise RuntimeError("test extraction barrier timed out")
        return original_extract(*args, **kwargs)

    monkeypatch.setattr(file_service, "extract_file_text", paused_extract)
    upload = asyncio.create_task(
        f.member.post(
            "/files",
            files={"file": ("revocation.txt", b"private revocation fixture", "text/plain")},
            headers=_headers(f.member),
        )
    )
    try:
        async with asyncio.timeout(15):
            while not entered.is_set():
                if upload.done():
                    response = await upload
                    pytest.fail(f"upload ended before barrier: {response.status_code}")
                await asyncio.sleep(0.01)
        changed = await f.source.patch(
            f"/memberships/{f.member_membership_id}",
            json={"role": "viewer"},
            headers={**_headers(f.source), "If-Match": f'"{f.member_revision}"'},
        )
        assert changed.status_code == 200
        release.set()
        denied = await asyncio.wait_for(upload, 15)
        assert denied.status_code == 401
        assert denied.json()["code"].startswith("workspace_session.")
    finally:
        release.set()
        if not upload.done():
            await asyncio.wait_for(upload, 25)
    storage = Path(app.state.config.file_storage_root)
    assert not [path for path in storage.rglob("*") if path.is_file()]
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute("SELECT status FROM uploaded_files").fetchall() == [
            ("processing",)
        ]
        assert connection.execute("SELECT COUNT(*) FROM file_extracts").fetchone()[0] == 0
    with pytest.raises(file_service.ApiError):
        file_service.create_file(
            app.state.config,
            stale,
            file_service.ParsedFilePart("stale.txt", "text/plain", b"stale context content"),
            request_id="test-file-stale",
            trace_id="test-file-stale",
        )
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM uploaded_files").fetchone()[0] == 1
    monkeypatch.setattr(file_service, "extract_file_text", original_extract)
    switched = await f.member.post(
        "/identity/workspace-switches",
        json={"workspace_id": f.source_workspace_id},
        headers=_headers(f.member, global_session=True),
    )
    assert switched.status_code == 200
    # The established Viewer matrix allows file.write; no role-name override.
    current = await f.member.post(
        "/files",
        files={"file": ("viewer.txt", b"current viewer content", "text/plain")},
        headers=_headers(f.member),
    )
    assert current.status_code == 201, current.json().get("code")
