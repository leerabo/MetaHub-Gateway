"""Real Platform sessions must survive a writer committing during read authentication."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import UTC, datetime, timedelta
from threading import Event

import pytest
import test_prd095_platform_iam as iam
from httpx import ASGITransport, AsyncClient
from metahub_control.data_governance import service as residency
from metahub_control.launch_governance import ga_service
from metahub_control.platform_access import service as platform
from metahub_control.platform_access import workspace_service as workspaces
from metahub_control.public_status import service as status
from metahub_shared.db import connect_database
from metahub_shared.runtime.errors import ApiError
from test_prd095_platform_iam import migrated_database as migrated_database


@pytest.fixture
async def platform_read_subject(monkeypatch, tmp_path, migrated_database):
    database_url, app = iam._prepare(monkeypatch, tmp_path, migrated_database)
    clock = [datetime.now(UTC).replace(microsecond=0)]
    monkeypatch.setattr(iam.identity_service, "_utcnow", lambda: clock[0])
    monkeypatch.setattr(platform, "_utcnow", lambda: clock[0])
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://testserver"
    ) as client:
        await iam._login(client, iam.ADMIN_EMAIL, iam.ADMIN_PASSWORD)
        secret = await iam._enroll(client, iam.ADMIN_PASSWORD, clock[0])
        clock[0] += timedelta(seconds=30)
        response = await client.post(
            "/platform/bootstrap",
            json={
                "current_password": iam.ADMIN_PASSWORD,
                "totp_code": iam._totp(secret, clock[0]),
                "recovery_secret": iam.RECOVERY_SECRET,
                "reason_code": "initial_platform_owner",
                "confirmed": True,
            },
            headers=iam._csrf(client, iam.GLOBAL_CSRF_COOKIE),
        )
        assert response.status_code == 200, response.text
        clock[0] += timedelta(seconds=30)
        response = await client.post(
            "/platform/sessions", json={"totp_code": iam._totp(secret, clock[0])},
            headers=iam._csrf(client, iam.GLOBAL_CSRF_COOKIE),
        )
        assert response.status_code == 200, response.text
        token = client.cookies.get(iam.PLATFORM_SESSION_COOKIE)
    clock[0] += timedelta(minutes=2)
    with closing(connect_database(database_url)) as connection:
        workspace_id = connection.execute(
            "SELECT workspace_id FROM workspace_tenant_bindings LIMIT 1"
        ).fetchone()[0]
    return app.state.config, token, workspace_id


def read_platform_page(subject, page):
    config, token, workspace_id = subject
    args = dict(session_token=token, request_id="req_contention", trace_id="trc_contention")
    if page == "authenticate":
        return platform.authenticate_platform_session_context(config, token)
    if page == "session":
        return platform.get_platform_session(config, **args)
    if page == "directory":
        return workspaces.list_platform_workspaces(
            config, limit=20, cursor=None, filters={}, **args
        )
    if page == "detail":
        return workspaces.get_platform_workspace(config, workspace_id=workspace_id, **args)
    if page == "health":
        return workspaces.get_platform_workspace_health(config, workspace_id=workspace_id, **args)
    if page == "residency":
        return residency.list_residency_evidence(
            config, workspace_id=None, component=None, outcome=None, cursor=None, limit=20, **args,
        )
    if page == "gates":
        return ga_service.list_ga_gates(
            config, platform_session_token=token,
            request_id="req_contention", trace_id="trc_contention",
        )
    if page == "status":
        return status.list_platform_status(config, **args)
    raise AssertionError(page)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "page",
    [
        "authenticate", "session", "directory", "detail",
        "health", "residency", "gates", "status",
    ],
)
async def test_platform_reads_survive_concurrent_commit(
    platform_read_subject, monkeypatch, page,
):
    config, _token, _workspace_id = platform_read_subject
    authority = platform._authority
    committed = Event()
    started = Event()
    writes = []

    def writer():
        with closing(connect_database(config.database_url)) as connection:
            started.set()
            connection.execute("BEGIN IMMEDIATE")
            # Keep the competing writer ahead of the reader's possible snapshot upgrade.
            import time

            time.sleep(0.15)
            connection.execute(
                "UPDATE platform_operators SET updated_at='2026-01-02T00:00:00Z'"
            )
            connection.commit()
        committed.set()

    with ThreadPoolExecutor(max_workers=1) as executor:
        def concurrent_authority(connection, operator_id):
            result = authority(connection, operator_id)
            if not writes:
                writes.append(executor.submit(writer))
                assert started.wait(timeout=1)
            return result

        monkeypatch.setattr(platform, "_authority", concurrent_authority)
        result = read_platform_page(platform_read_subject, page)
        assert result is not None
        assert len(writes) == 1
        writes[0].result(timeout=5)
    assert committed.is_set()


@pytest.mark.anyio
async def test_platform_touch_rechecks_revocation_after_concurrent_commit(
    platform_read_subject, monkeypatch,
):
    config, _token, _workspace_id = platform_read_subject
    authority = platform._authority
    changed = False

    def revoking_authority(connection, operator_id):
        nonlocal changed
        result = authority(connection, operator_id)
        if not changed:
            changed = True
            with closing(connect_database(config.database_url)) as writer:
                writer.execute("BEGIN IMMEDIATE")
                writer.execute(
                    "UPDATE platform_operators SET status='disabled',revision=revision+1 "
                    "WHERE operator_id=?", (operator_id,),
                )
                writer.commit()
        return result

    monkeypatch.setattr(platform, "_authority", revoking_authority)
    with pytest.raises(ApiError) as denied:
        read_platform_page(platform_read_subject, "session")
    assert denied.value.code == "platform.session_revoked"
    with closing(connect_database(config.database_url)) as connection:
        assert connection.execute("SELECT status FROM platform_sessions").fetchone()[0] == "revoked"
