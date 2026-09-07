"""Workspace Provider observation and session-owned manual health transactions."""

import asyncio
import sqlite3
import threading
from contextlib import closing
from datetime import UTC, datetime, timedelta

import pytest
from metahub_control.auth.resource_transactions import ResourceWriteTransaction
from metahub_control.provider_health import routes as health_routes
from metahub_control.provider_health import service as health
from metahub_control.provider_status import routes as status_routes
from metahub_control.provider_status import service as status
from metahub_control.workspace_access import service as workspace_access
from metahub_shared.runtime.errors import ApiError
from test_prd147_catalog_transactions import (  # noqa: F401
    _assert_closed,
    _attempt,
    _facts,
    _track_connections,
    catalog_subjects,
    file_subjects,
    migrated_baseline,
)
from test_prd147_workspace_files import _headers


@pytest.fixture
async def observation_subjects(catalog_subjects):  # noqa: F811
    app, *_rest = catalog_subjects
    app.include_router(health_routes.router)
    app.include_router(status_routes.router)
    return catalog_subjects


def _call(subjects, operation="probe"):
    app, _database, _fixtures, provider_id, context = subjects
    common = {"request_id": "observation-test", "trace_id": "observation-test"}
    if operation == "probe":
        return health.probe_provider(
            app.state.config, context, provider_id, trigger_type="manual", **common,
        )
    reader = health.list_provider_health if operation == "health" else status.list_provider_statuses
    return reader(
        app.state.config, context, provider_id=provider_id, provider_type=None,
        health_status=None, provider_status=None, limit=50, offset=0, **common,
    )


@pytest.mark.anyio
async def test_status_accepts_actual_workspace_cookie(observation_subjects):
    _app, _database, fixtures, provider_id, _context = observation_subjects
    response = await fixtures.source.get(f"/status/providers?provider_id={provider_id}")
    assert response.status_code == 200, response.json().get("code")
    assert response.json()["total"] == 1
    assert "no-store" in response.headers["cache-control"]


@pytest.mark.anyio
async def test_manual_probe_rejects_revoked_context_before_network(
    observation_subjects, monkeypatch,
):
    _app, database, fixtures, _provider_id, _context = observation_subjects
    logout = await fixtures.source.post(
        "/identity/logout", headers=_headers(fixtures.source, global_session=True),
    )
    assert logout.status_code == 200
    calls = []

    def probe(*_args, **_kwargs):
        calls.append(True)
        return [_attempt()]

    monkeypatch.setattr(health, "_perform_probe", probe)
    before = _facts(database)
    with pytest.raises(ApiError) as rejected:
        _call(observation_subjects)
    assert rejected.value.code == "workspace_session.revoked"
    assert not calls and _facts(database) == before


@pytest.mark.anyio
@pytest.mark.parametrize("operation", ["health", "status"])
async def test_observation_reads_reject_revoked_context(observation_subjects, operation):
    _app, database, fixtures, _provider_id, _context = observation_subjects
    logout = await fixtures.source.post(
        "/identity/logout", headers=_headers(fixtures.source, global_session=True),
    )
    assert logout.status_code == 200
    before = _facts(database)
    with pytest.raises(ApiError) as rejected:
        _call(observation_subjects, operation)
    assert rejected.value.code == "workspace_session.revoked"
    assert _facts(database) == before


@pytest.mark.anyio
async def test_observation_cookie_scope_probe_and_viewer_guards(observation_subjects, monkeypatch):
    _app, database, fixtures, provider_id, _context = observation_subjects
    calls = []

    def transport(*_args, **_kwargs):
        calls.append(True)
        return [_attempt()]

    monkeypatch.setattr(health, "_perform_probe", transport)
    for endpoint in ("/provider-health", "/status/providers"):
        for client, count in ((fixtures.source, 1), (fixtures.target, 0)):
            response = await client.get(f"{endpoint}?provider_id={provider_id}")
            assert response.status_code == 200, response.json().get("code")
            assert response.json()["total"] == count
            assert "no-store" in response.headers["cache-control"]
        anonymous = await fixtures.anonymous.get(endpoint)
        assert anonymous.status_code == 401
        mixed = await fixtures.anonymous.get(endpoint, headers={
            **_headers(fixtures.source, attack=True), "Authorization": "Bearer invalid",
        })
        assert mixed.status_code == 400
    before = _facts(database)
    owner_headers = _headers(fixtures.source, attack=True)
    for headers, expected in (
        ({}, 401),
        ({**owner_headers, "X-CSRF-Token": "invalid"}, 403),
        ({**owner_headers, "Origin": "https://foreign.test"}, 403),
        ({**owner_headers, "Authorization": "Bearer invalid"}, 400),
        (_headers(fixtures.target, attack=True), 404),
    ):
        denied = await fixtures.anonymous.post(
            f"/providers/{provider_id}/probe", json={}, headers=headers,
        )
        assert denied.status_code == expected, denied.json().get("code")
    assert not calls and _facts(database) == before
    opened = _track_connections(monkeypatch)
    completed = await fixtures.source.post(
        f"/providers/{provider_id}/probe", json={}, headers=_headers(fixtures.source),
    )
    assert completed.status_code == 200, completed.json().get("code")
    assert calls == [True]
    assert completed.json()["provider_health"]["health_status"] == "healthy"
    _assert_closed(opened)
    with closing(sqlite3.connect(database)) as connection:
        logs = connection.execute("SELECT COUNT(*) FROM provider_health_probe_logs").fetchone()[0]
        assert logs == 1
        assert connection.execute("SELECT COUNT(*) FROM provider_preflight_runs").fetchone()[0] == 0
    changed = await fixtures.source.patch(
        f"/memberships/{fixtures.member_membership_id}", json={"role": "viewer"},
        headers={**_headers(fixtures.source), "If-Match": f'"{fixtures.member_revision}"'},
    )
    assert changed.status_code == 200
    switched = await fixtures.member.post(
        "/identity/workspace-switches", json={"workspace_id": fixtures.source_workspace_id},
        headers=_headers(fixtures.member, global_session=True),
    )
    assert switched.status_code == 200
    for endpoint in ("/provider-health", "/status/providers"):
        assert (await fixtures.member.get(endpoint)).status_code == 403
    denied = await fixtures.member.post(
        f"/providers/{provider_id}/probe", json={}, headers=_headers(fixtures.member),
    )
    assert denied.status_code == 403 and calls == [True]


@pytest.mark.anyio
@pytest.mark.parametrize("change", ["logout", "downgrade"])
async def test_manual_probe_http_revocation_during_network(
    observation_subjects, monkeypatch, change,
):
    _app, database, fixtures, provider_id, _context = observation_subjects
    entered, release = threading.Event(), threading.Event()
    opened = _track_connections(monkeypatch)

    def transport(*_args, **_kwargs):
        _assert_closed(opened)
        entered.set()
        if not release.wait(15):
            raise RuntimeError("observation barrier timed out")
        return [_attempt()]

    monkeypatch.setattr(health, "_perform_probe", transport)
    pending = asyncio.create_task(fixtures.member.post(
        f"/providers/{provider_id}/probe", json={}, headers=_headers(fixtures.member),
    ))
    try:
        async with asyncio.timeout(10):
            while not entered.is_set():
                if pending.done():
                    response = await pending
                    pytest.fail(f"probe ended before barrier: {response.status_code}")
                await asyncio.sleep(0.01)
        if change == "logout":
            changed = await fixtures.member.post(
                "/identity/logout", headers=_headers(fixtures.member, global_session=True),
            )
        else:
            changed = await fixtures.source.patch(
                f"/memberships/{fixtures.member_membership_id}", json={"role": "viewer"},
                headers={**_headers(fixtures.source), "If-Match": f'"{fixtures.member_revision}"'},
            )
        assert changed.status_code == 200
        expected = _facts(database, ignore_lease=True)
        release.set()
        response = await asyncio.wait_for(pending, 15)
        assert response.status_code == 401
        assert response.json()["code"].startswith("workspace_session.")
    finally:
        release.set()
        if not pending.done():
            await asyncio.wait_for(pending, 20)
    assert _facts(database) == expected
    _assert_closed(opened)


@pytest.mark.anyio
@pytest.mark.parametrize("operation", ["health", "status", "probe"])
async def test_observation_reauthenticates_before_return_or_commit(
    observation_subjects, monkeypatch, operation,
):
    _app, database, _fixtures, _provider_id, _context = observation_subjects
    original = ResourceWriteTransaction.commit
    commits = []

    def revoke(transaction, *args, **kwargs):
        commits.append(True)
        if operation != "probe" or len(commits) == 2:
            workspace_access._revoke_workspace_session(
                transaction.connection, transaction.context.session.session_id,
                datetime.now(UTC), "authority_changed",
            )
        return original(transaction, *args, **kwargs)

    monkeypatch.setattr(ResourceWriteTransaction, "commit", revoke)
    monkeypatch.setattr(health, "_perform_probe", lambda *a, **k: [_attempt()])
    before = _facts(database)
    with pytest.raises(ApiError) as rejected:
        _call(observation_subjects, operation)
    assert rejected.value.code == "workspace_session.revoked"
    assert _facts(database) == before


@pytest.mark.anyio
async def test_status_rechecks_second_permission_at_snapshot_end(observation_subjects, monkeypatch):
    _app, database, _fixtures, _provider_id, _context = observation_subjects
    original = status.require_permission
    checks = []

    def reject_second(context, permission):
        checks.append(permission)
        if len(checks) == 2:
            raise ApiError("auth.permission_denied")
        return original(context, permission)

    monkeypatch.setattr(status, "require_permission", reject_second)
    before = _facts(database)
    with pytest.raises(ApiError) as rejected:
        _call(observation_subjects, "status")
    assert rejected.value.code == "auth.permission_denied"
    assert checks == ["request_log.read", "request_log.read"] and _facts(database) == before


@pytest.mark.anyio
@pytest.mark.parametrize("fault", ["lease", "generation", "commit", "expiry", "network"])
async def test_manual_probe_failure_preserves_facts_and_competing_lease(
    observation_subjects, monkeypatch, fault,
):
    _app, database, _fixtures, provider_id, _context = observation_subjects
    expected = [_facts(database)]

    def transport(*_args, **_kwargs):
        if fault == "network":
            raise sqlite3.OperationalError("injected network-stage failure")
        if fault in {"lease", "generation"}:
            with closing(sqlite3.connect(database)) as connection:
                if fault == "lease":
                    connection.execute(
                        "UPDATE providers SET probe_lease_token='other-lease' WHERE provider_id=?",
                        (provider_id,),
                    )
                else:
                    connection.execute(
                        "UPDATE providers SET health_generation=health_generation+1,"
                        "probe_lease_token=NULL,probe_lease_until=NULL WHERE provider_id=?",
                        (provider_id,),
                    )
                connection.commit()
            expected[0] = _facts(database)
        return [_attempt()]

    monkeypatch.setattr(health, "_perform_probe", transport)
    original = health._complete_probe

    def complete(*args, **kwargs):
        result = original(*args, **kwargs)
        if fault == "commit":
            raise sqlite3.OperationalError("injected pre-commit failure")
        if fault == "expiry":
            future = datetime.now(UTC) + timedelta(hours=1)
            monkeypatch.setattr(health, "_utcnow", lambda: future)
        return result

    monkeypatch.setattr(health, "_complete_probe", complete)
    with pytest.raises((ApiError, sqlite3.OperationalError)):
        _call(observation_subjects)
    assert _facts(database) == expected[0]


@pytest.mark.anyio
async def test_scheduled_probe_keeps_machine_authority_and_closes_connections(
    observation_subjects, monkeypatch,
):
    app, database, fixtures, provider_id, context = observation_subjects
    logout = await fixtures.source.post(
        "/identity/logout", headers=_headers(fixtures.source, global_session=True),
    )
    assert logout.status_code == 200
    with closing(sqlite3.connect(database)) as connection:
        connection.execute(
            "UPDATE providers SET next_probe_at=? WHERE provider_id=?",
            (health._db_time(datetime.now(UTC) - timedelta(seconds=1)), provider_id),
        )
        connection.commit()
    calls = []
    opened = _track_connections(monkeypatch)

    def transport(*_args, **_kwargs):
        _assert_closed(opened)
        calls.append(True)
        return [_attempt()]

    monkeypatch.setattr(health, "_perform_probe", transport)
    result = health.probe_provider_for_tenant(
        app.state.config, context.user.tenant_id, provider_id, trigger_type="scheduled",
        request_id="scheduled-observation-test", trace_id="scheduled-observation-test",
    )
    assert result.result_status == "success" and calls == [True]
    _assert_closed(opened)
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute(
            "SELECT trigger_type FROM provider_health_probe_logs WHERE provider_id=?",
            (provider_id,),
        ).fetchall() == [("scheduled",)]
    with pytest.raises(ApiError) as rejected:
        health.probe_provider_for_tenant(
            app.state.config, context.user.tenant_id, provider_id, trigger_type="scheduled",
            request_id="not-due-observation-test", trace_id="not-due-observation-test",
        )
    assert rejected.value.code == "provider_health.probe_not_due" and calls == [True]
    _assert_closed(opened)
