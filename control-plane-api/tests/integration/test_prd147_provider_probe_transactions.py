"""Real Workspace authorization around the legacy reachability network boundary."""

import asyncio
import hashlib
import json
import secrets
import sqlite3
import threading
from contextlib import closing
from datetime import UTC, datetime, timedelta

import pytest
from metahub_control.auth.resource_transactions import ResourceWriteTransaction
from metahub_control.provider_health import service as health
from metahub_control.providers import models, routes, service
from metahub_control.workspace_access import service as workspace_access
from metahub_shared.runtime.errors import ApiError
from test_prd147_workspace_files import (  # noqa: F401
    WORKSPACE_SESSION,
    _headers,
    file_subjects,
    migrated_baseline,
)


@pytest.fixture
async def probe_subjects(file_subjects):  # noqa: F811
    app, database, fixtures = file_subjects
    app.include_router(routes.router)
    app.include_router(routes.provider_keys_router)
    response = await fixtures.source.post(
        "/providers",
        json={
            "provider_code": "probe_transaction",
            "name": "Probe",
            "base_url": "https://provider.test/v1",
        },
        headers=_headers(fixtures.source),
    )
    assert response.status_code == 201, response.json().get("code")
    context = workspace_access.authenticate_workspace_session_as_auth_context(
        app.state.config, fixtures.source.cookies.get(WORKSPACE_SESSION)
    )
    return app, database, fixtures, response.json()["provider"]["provider_id"], context


def _facts(database):
    with closing(sqlite3.connect(database)) as connection:
        return {
            table: hashlib.sha256(
                json.dumps(
                    sorted(connection.execute(f'SELECT * FROM "{table}"').fetchall(), key=repr)
                ).encode()
            ).hexdigest()
            for table in (
                "providers",
                "provider_credentials",
                "provider_health_probe_logs",
                "audit_logs",
            )
        }


def _attempt(checked_at=None):
    return health._fixed_attempt(
        "success", "unverified", None, "Endpoint is reachable.", checked_at or datetime.now(UTC)
    )


def _call(subjects, context=None):
    app, _database, _fixtures, provider_id, owner = subjects
    return service.test_provider_connection(
        app.state.config,
        context or owner,
        provider_id,
        models.TestProviderRequest(),
        request_id="provider-probe-test",
        trace_id="provider-probe-test",
    )


def _track_connections(monkeypatch):
    opened = []
    original = service._connect

    class TrackedConnection:
        def __init__(self, connection):
            self.connection = connection
            self.closed = False

        def __getattr__(self, name):
            return getattr(self.connection, name)

        def close(self):
            self.connection.close()
            # Verify closure in the owning thread, not from the HTTP event loop.
            with pytest.raises(sqlite3.ProgrammingError, match="closed"):
                self.connection.execute("SELECT 1")
            self.closed = True

    def connect(config):
        connection = TrackedConnection(original(config))
        opened.append(connection)
        return connection

    monkeypatch.setattr(service, "_connect", connect)
    return opened


def _assert_closed(connections):
    for connection in connections:
        assert connection.closed


@pytest.mark.anyio
async def test_probe_rejects_revoked_context_before_network(probe_subjects, monkeypatch):
    _app, database, fixtures, _provider_id, _context = probe_subjects
    calls = []
    monkeypatch.setattr(
        health, "probe_provider_reachability", lambda *a, **k: calls.append(True) or _attempt()
    )
    opened = _track_connections(monkeypatch)
    logout = await fixtures.source.post(
        "/identity/logout", headers=_headers(fixtures.source, global_session=True)
    )
    assert logout.status_code == 200
    before = _facts(database)
    with pytest.raises(ApiError) as rejected:
        _call(probe_subjects)
    assert rejected.value.code == "workspace_session.revoked"
    assert calls == [] and _facts(database) == before
    _assert_closed(opened)


@pytest.mark.anyio
@pytest.mark.parametrize("change", ["endpoint", "endpoint_aba", "status", "credential"])
async def test_probe_discards_changed_configuration(probe_subjects, monkeypatch, change):
    app, database, _fixtures, provider_id, context = probe_subjects
    after_change = []
    opened = _track_connections(monkeypatch)

    def probe(*args, **kwargs):
        if change == "credential":
            service.create_provider_key(
                app.state.config,
                context,
                provider_id,
                models.CreateProviderKeyRequest(
                    name="Probe key", plain_value=secrets.token_urlsafe(32)
                ),
                request_id="probe-change",
                trace_id="probe-change",
            )
        else:
            payload = (
                {"status": "disabled"}
                if change == "status"
                else {"base_url": "https://new-provider.test/v1"}
            )
            service.update_provider(
                app.state.config,
                context,
                provider_id,
                models.UpdateProviderRequest(**payload),
                request_id="probe-change",
                trace_id="probe-change",
            )
            if change == "endpoint_aba":
                service.update_provider(
                    app.state.config,
                    context,
                    provider_id,
                    models.UpdateProviderRequest(base_url="https://provider.test/v1"),
                    request_id="probe-change",
                    trace_id="probe-change",
                )
        after_change.append(_facts(database))
        return _attempt()

    monkeypatch.setattr(health, "probe_provider_reachability", probe)
    with pytest.raises(ApiError) as rejected:
        _call(probe_subjects)
    assert rejected.value.code == "provider_health.probe_superseded"
    assert _facts(database) == after_change[0]
    _assert_closed(opened)


@pytest.mark.anyio
async def test_probe_cannot_overwrite_newer_result_in_same_second(probe_subjects, monkeypatch):
    _app, database, _fixtures, _provider_id, _context = probe_subjects
    start = datetime.now(UTC).replace(microsecond=100000)
    monkeypatch.setattr(health, "probe_provider_reachability", lambda *a, **k: _attempt(start))
    _call(probe_subjects)
    newer_facts = []

    def older_probe(*args, **kwargs):
        with monkeypatch.context() as nested:
            nested.setattr(
                health,
                "probe_provider_reachability",
                lambda *a, **k: _attempt(start + timedelta(microseconds=2)),
            )
            _call(probe_subjects)
        newer_facts.append(_facts(database))
        return _attempt(start + timedelta(microseconds=1))

    monkeypatch.setattr(health, "probe_provider_reachability", older_probe)
    with pytest.raises(ApiError) as rejected:
        _call(probe_subjects)
    assert rejected.value.code == "provider_health.probe_superseded"
    assert _facts(database) == newer_facts[0]


@pytest.mark.anyio
@pytest.mark.parametrize("change", ["logout", "downgrade"])
async def test_http_probe_rechecks_authority_after_network_wait(
    probe_subjects, monkeypatch, change
):
    _app, database, fixtures, provider_id, _context = probe_subjects
    opened = _track_connections(monkeypatch)
    entered, release = threading.Event(), threading.Event()

    def probe(*args, **kwargs):
        assert len(opened) == 1
        _assert_closed(opened)
        entered.set()
        if not release.wait(15):
            raise RuntimeError("test probe barrier timed out")
        return _attempt()

    monkeypatch.setattr(health, "probe_provider_reachability", probe)
    pending = asyncio.create_task(
        fixtures.member.post(
            f"/providers/{provider_id}/test", json={}, headers=_headers(fixtures.member)
        )
    )
    try:
        async with asyncio.timeout(10):
            while not entered.is_set():
                if pending.done():
                    response = await pending
                    pytest.fail(f"probe ended before network barrier: {response.status_code}")
                await asyncio.sleep(0.01)
        if change == "logout":
            changed = await fixtures.member.post(
                "/identity/logout", headers=_headers(fixtures.member, global_session=True)
            )
        else:
            changed = await fixtures.source.patch(
                f"/memberships/{fixtures.member_membership_id}",
                json={"role": "viewer"},
                headers={**_headers(fixtures.source), "If-Match": f'"{fixtures.member_revision}"'},
            )
        assert changed.status_code == 200, changed.json().get("code")
        after_change = _facts(database)
        release.set()
        denied = await asyncio.wait_for(pending, 10)
        assert denied.status_code == 401 and denied.json()["code"].startswith("workspace_session.")
        assert _facts(database) == after_change
        assert len(opened) == 2
        _assert_closed(opened)
    finally:
        release.set()
        if not pending.done():
            await asyncio.wait_for(pending, 20)


@pytest.mark.anyio
async def test_probe_cookie_boundary_and_current_owner(probe_subjects, monkeypatch):
    _app, database, fixtures, provider_id, _context = probe_subjects
    calls = []
    opened = _track_connections(monkeypatch)

    def probe(*args, **kwargs):
        calls.append(True)
        _assert_closed(opened)
        return _attempt()

    monkeypatch.setattr(health, "probe_provider_reachability", probe)
    before = _facts(database)
    headers = _headers(fixtures.source, attack=True)
    for supplied, expected in (
        ({}, 401),
        ({**headers, "X-CSRF-Token": "invalid"}, 403),
        ({**headers, "Origin": "https://foreign.test"}, 403),
        ({**headers, "Authorization": "Bearer invalid"}, 400),
        (_headers(fixtures.target, attack=True), 404),
    ):
        response = await fixtures.anonymous.post(
            f"/providers/{provider_id}/test", json={}, headers=supplied
        )
        assert response.status_code == expected, response.json().get("code")
    assert calls == [] and _facts(database) == before
    accepted = await fixtures.source.post(
        f"/providers/{provider_id}/test", json={}, headers=_headers(fixtures.source)
    )
    assert accepted.status_code == 200, accepted.json().get("code")
    assert accepted.json()["provider"]["health_status"] == "healthy"
    assert len(calls) == 1
    _assert_closed(opened)


@pytest.mark.anyio
@pytest.mark.parametrize("failure", ["expiry", "commit"])
async def test_probe_commit_failure_rolls_back_and_closes(probe_subjects, monkeypatch, failure):
    _app, database, _fixtures, _provider_id, _context = probe_subjects
    opened = _track_connections(monkeypatch)
    emitted = []
    monkeypatch.setattr(health, "probe_provider_reachability", lambda *a, **k: _attempt())
    monkeypatch.setattr(service, "_log_health_checked", lambda *a, **k: emitted.append(True))
    original_commit = ResourceWriteTransaction.commit
    future = workspace_access._utcnow() + timedelta(days=2)

    def fail_commit(_connection):
        raise sqlite3.OperationalError("test probe commit failure")

    def commit(transaction, committer=None):
        if failure == "commit":
            return original_commit(transaction, fail_commit)
        with monkeypatch.context() as clock:
            clock.setattr(workspace_access, "_utcnow", lambda: future)
            return original_commit(transaction, committer)

    monkeypatch.setattr(ResourceWriteTransaction, "commit", commit)
    before = _facts(database)
    if failure == "expiry":
        with pytest.raises(ApiError) as rejected:
            _call(probe_subjects)
        assert rejected.value.code == "workspace_session.expired"
    else:
        with pytest.raises(sqlite3.OperationalError, match="test probe commit failure"):
            _call(probe_subjects)
    assert _facts(database) == before and emitted == []
    assert len(opened) == 2
    _assert_closed(opened)


@pytest.mark.anyio
@pytest.mark.parametrize("competition", ["existing_lease", "inflight_lease", "completed_probe"])
async def test_probe_respects_health_worker_lease_and_watermark(
    probe_subjects, monkeypatch, competition
):
    app, database, _fixtures, provider_id, context = probe_subjects
    opened = _track_connections(monkeypatch)
    worker_connections = []
    original_connect = health._connect

    def connect(config):
        connection = original_connect(config)
        worker_connections.append(connection)
        return connection

    monkeypatch.setattr(health, "_connect", connect)
    after_worker = []

    def advance_worker():
        claim, _credentials = health._claim_provider(
            app.state.config, context.user.tenant_id, provider_id, "manual"
        )
        if competition == "completed_probe":
            health._complete_probe(
                app.state.config,
                claim,
                [_attempt()],
                result_status="success",
                trigger_type="manual",
                request_id="health-worker-test",
                trace_id="health-worker-test",
            )
        after_worker.append(_facts(database))

    def probe(*args, **kwargs):
        if competition == "existing_lease":
            pytest.fail("existing health lease must block network access")
        advance_worker()
        return _attempt()

    monkeypatch.setattr(health, "probe_provider_reachability", probe)
    try:
        if competition == "existing_lease":
            advance_worker()
        with pytest.raises(ApiError) as rejected:
            _call(probe_subjects)
        expected = "probe_in_progress" if competition == "existing_lease" else "probe_superseded"
        assert rejected.value.code == f"provider_health.{expected}"
        assert _facts(database) == after_worker[0]
        _assert_closed(opened)
    finally:
        # The existing worker helpers do not own their connections yet.
        for connection in worker_connections:
            connection.close()
