"""Real Workspace Catalog authorization and atomic health/preflight persistence."""

import asyncio
import hashlib
import hmac
import json
import secrets
import sqlite3
import threading
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from metahub_control.auth.resource_transactions import ResourceWriteTransaction
from metahub_control.provider_catalog import models, routes, service
from metahub_control.provider_health import service as health
from metahub_control.providers import models as provider_models
from metahub_control.providers import service as providers
from metahub_control.workspace_access import service as workspace_access
from metahub_shared.runtime.errors import ApiError
from test_prd147_workspace_files import (  # noqa: F401
    WORKSPACE_SESSION,
    _headers,
    file_subjects,
    migrated_baseline,
)


def _payload(code="catalog_transaction"):
    return models.CatalogProviderCreateRequest(
        entry_code="deepseek", version="2026.08.26.1", region_code="cn-mainland",
        capability_codes=["chat"], provider_code=code, name="Catalog transaction", confirmed=True,
    )


@pytest.fixture
async def catalog_subjects(file_subjects):  # noqa: F811
    app, database, fixtures = file_subjects
    app.include_router(routes.router)
    response = await fixtures.source.post(
        "/providers/from-catalog", json=_payload().model_dump(mode="json"),
        headers={**_headers(fixtures.source), "Idempotency-Key": "catalog-transaction-create"},
    )
    assert response.status_code == 201, response.json().get("code")
    context = workspace_access.authenticate_workspace_session_as_auth_context(
        app.state.config, fixtures.source.cookies.get(WORKSPACE_SESSION)
    )
    return app, database, fixtures, response.json()["instance"]["provider_id"], context


def _call(subjects, operation="preflight", context=None):
    app, _database, _fixtures, provider_id, owner = subjects
    common = {"request_id": "catalog-test", "trace_id": "catalog-test"}
    context = context or owner
    if operation == "list":
        return service.list_provider_catalog(
            app.state.config, context, status=None, vendor_code=None, capability_code=None,
            region_code=None, **common,
        )
    if operation == "compatibility":
        return service.get_catalog_compatibility(app.state.config, context, "deepseek", **common)
    if operation == "create":
        return service.create_catalog_provider(
            app.state.config, context, _payload("catalog_other"),
            idempotency_key="catalog-transaction-other", **common,
        )
    return service.preflight_catalog_provider(
        app.state.config, context, provider_id, models.ProviderPreflightRequest(),
        idempotency_key="catalog-transaction-preflight", **common,
    )


def _facts(database, *, ignore_lease=False):
    with closing(sqlite3.connect(database)) as connection:
        connection.row_factory = sqlite3.Row
        result = {}
        for table in (
            "providers", "provider_credentials", "provider_health_probe_logs",
            "provider_catalog_instances", "provider_preflight_runs",
            "workspace_provider_catalog_commands", "provider_catalog_events",
            "provider_catalog_event_outbox", "audit_logs",
        ):
            rows = [dict(row) for row in connection.execute(f'SELECT * FROM "{table}"')]
            if table == "providers" and ignore_lease:
                for row in rows:
                    row["probe_lease_token"] = row["probe_lease_until"] = None
            result[table] = hashlib.sha256(
                json.dumps(sorted(rows, key=repr), sort_keys=True).encode()
            ).hexdigest()
        return result


def _attempt():
    return health._fixed_attempt(
        "success", "unverified", None, "Provider probe succeeded.", datetime.now(UTC)
    )


def _track_connections(monkeypatch):
    opened = []

    class TrackedConnection:
        def __init__(self, connection):
            self.connection = connection
            self.closed = False

        def __getattr__(self, name):
            return getattr(self.connection, name)

        def close(self):
            self.connection.close()
            try:
                self.connection.execute("SELECT 1")
            except sqlite3.ProgrammingError as error:
                assert "closed" in str(error)
                self.closed = True

    for module in (service, health):
        original = module._connect

        def connect(config, original=original):
            connection = TrackedConnection(original(config))
            opened.append(connection)
            return connection

        monkeypatch.setattr(module, "_connect", connect)
    return opened


def _assert_closed(opened):
    assert opened
    for connection in opened:
        if not connection.closed:
            # Non-network routes release their request-owned facade on HTTP response teardown.
            with pytest.raises(sqlite3.ProgrammingError, match="closed"):
                connection.connection.execute("SELECT 1")
            connection.closed = True


@pytest.mark.anyio
async def test_catalog_real_cookie_scope_and_preflight_replay(catalog_subjects, monkeypatch):
    _app, database, fixtures, provider_id, _context = catalog_subjects
    opened = _track_connections(monkeypatch)
    network_calls = []

    def transport(*args, **kwargs):
        _assert_closed(opened)
        network_calls.append(True)
        return [_attempt()]

    monkeypatch.setattr(health, "_perform_probe", transport)
    listed = await fixtures.source.get("/provider-catalog?capability_code=chat")
    assert listed.status_code == 200 and "no-store" in listed.headers["cache-control"]
    for client, expected in ((fixtures.source, 1), (fixtures.target, 0)):
        response = await client.get("/provider-catalog/deepseek/compatibility")
        assert response.status_code == 200 and response.json()["current_instances"] == expected
    foreign = await fixtures.target.post(
        f"/providers/{provider_id}/preflight", json={},
        headers={**_headers(fixtures.target), "Idempotency-Key": "catalog-foreign-preflight"},
    )
    assert foreign.status_code == 404
    assert not network_calls
    for replay in (False, True):
        response = await fixtures.source.post(
            f"/providers/{provider_id}/preflight", json={},
            headers={**_headers(fixtures.source), "Idempotency-Key": "catalog-cookie-preflight"},
        )
        assert response.status_code == 200, response.json().get("code")
        assert response.json()["idempotent_replay"] is replay
        assert response.json()["run"]["status"] == "succeeded"
        _assert_closed(opened)
    assert network_calls == [True]
    with closing(sqlite3.connect(database)) as connection:
        for table in ("provider_preflight_runs", "provider_health_probe_logs"):
            assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 1
        assert connection.execute(
            "SELECT probe_lease_token,probe_lease_until,health_status FROM providers "
            "WHERE provider_id=?", (provider_id,),
        ).fetchone() == (None, None, "healthy")
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.anyio
@pytest.mark.parametrize("operation", ["list", "compatibility", "create", "preflight"])
async def test_catalog_rejects_revoked_context_without_legacy_fallback(
    catalog_subjects, monkeypatch, operation,
):
    _app, database, fixtures, _provider_id, _context = catalog_subjects
    opened = _track_connections(monkeypatch)
    monkeypatch.setattr(health, "_perform_probe", lambda *a, **k: pytest.fail("unexpected network"))
    logout = await fixtures.source.post(
        "/identity/logout", headers=_headers(fixtures.source, global_session=True)
    )
    assert logout.status_code == 200
    before = _facts(database)
    with pytest.raises(ApiError) as rejected:
        _call(catalog_subjects, operation)
    assert rejected.value.code == "workspace_session.revoked"
    assert _facts(database) == before
    _assert_closed(opened)


@pytest.mark.anyio
@pytest.mark.parametrize("operation", ["list", "compatibility", "create", "preflight"])
async def test_catalog_reauthenticates_at_commit(catalog_subjects, monkeypatch, operation):
    _app, database, _fixtures, _provider_id, _context = catalog_subjects
    opened = _track_connections(monkeypatch)
    original = ResourceWriteTransaction.commit

    def revoke_and_commit(transaction, *args, **kwargs):
        workspace_access._revoke_workspace_session(
            transaction.connection, transaction.context.session.session_id,
            datetime.now(UTC), "authority_changed",
        )
        return original(transaction, *args, **kwargs)

    monkeypatch.setattr(ResourceWriteTransaction, "commit", revoke_and_commit)
    before = _facts(database)
    with pytest.raises(ApiError) as rejected:
        _call(catalog_subjects, operation)
    assert rejected.value.code == "workspace_session.revoked"
    assert _facts(database) == before
    _assert_closed(opened)


@pytest.mark.anyio
@pytest.mark.parametrize("change", ["logout", "downgrade"])
async def test_preflight_http_revocation_during_network(catalog_subjects, monkeypatch, change):
    _app, database, fixtures, provider_id, _context = catalog_subjects
    opened = _track_connections(monkeypatch)
    entered, release = threading.Event(), threading.Event()

    def transport(*args, **kwargs):
        _assert_closed(opened)
        entered.set()
        if not release.wait(15):
            raise RuntimeError("catalog test barrier timed out")
        return [_attempt()]

    monkeypatch.setattr(health, "_perform_probe", transport)
    before = _facts(database)
    pending = asyncio.create_task(fixtures.member.post(
        f"/providers/{provider_id}/preflight", json={},
        headers={**_headers(fixtures.member), "Idempotency-Key": "catalog-member-preflight"},
    ))
    try:
        async with asyncio.timeout(10):
            while not entered.is_set():
                if pending.done():
                    response = await pending
                    pytest.fail(f"request ended before network: {response.status_code}")
                await asyncio.sleep(0.01)
        if change == "logout":
            changed = await fixtures.member.post(
                "/identity/logout", headers=_headers(fixtures.member, global_session=True)
            )
        else:
            changed = await fixtures.source.patch(
                f"/memberships/{fixtures.member_membership_id}", json={"role": "viewer"},
                headers={**_headers(fixtures.source), "If-Match": f'"{fixtures.member_revision}"'},
            )
        assert changed.status_code == 200
        expected = _facts(database, ignore_lease=True)
        release.set()
        denied = await asyncio.wait_for(pending, 15)
        assert denied.status_code == 401 and denied.json()["code"].startswith("workspace_session.")
    finally:
        release.set()
        if not pending.done():
            await asyncio.wait_for(pending, 20)
    assert _facts(database) == expected
    assert _facts(database)["provider_preflight_runs"] == before["provider_preflight_runs"]
    _assert_closed(opened)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "change", ["endpoint_aba", "credential", "generation", "capability", "lease", "expiry"],
)
async def test_preflight_rejects_changed_claim(catalog_subjects, monkeypatch, change):
    app, database, _fixtures, provider_id, context = catalog_subjects
    expected = []

    def transport(*args, **kwargs):
        if change == "endpoint_aba":
            for url in ("https://other.test/v1", "https://api.deepseek.com/v1"):
                providers.update_provider(
                    app.state.config, context, provider_id,
                    provider_models.UpdateProviderRequest(base_url=url),
                    request_id="catalog-change", trace_id="catalog-change",
                )
        elif change == "credential":
            providers.create_provider_key(
                app.state.config, context, provider_id,
                provider_models.CreateProviderKeyRequest(
                    name="Changed Catalog key", plain_value=secrets.token_urlsafe(32),
                ),
                request_id="catalog-change", trace_id="catalog-change",
            )
        else:
            with closing(sqlite3.connect(database)) as connection:
                if change == "generation":
                    connection.execute(
                        "UPDATE providers SET health_generation=health_generation+1,"
                        "probe_lease_token=NULL,probe_lease_until=NULL WHERE provider_id=?",
                        (provider_id,),
                    )
                elif change == "capability":
                    # Exercise application fencing after deliberate damage to the immutable guard.
                    connection.execute("DROP TRIGGER trg_provider_catalog_instances_update_prd119")
                    connection.execute(
                        "UPDATE provider_catalog_instances SET capability_codes_json='[\"tools\"]' "
                        "WHERE provider_id=?", (provider_id,),
                    )
                elif change == "lease":
                    connection.execute(
                        "UPDATE providers SET probe_lease_token='another-owner' "
                        "WHERE provider_id=?",
                        (provider_id,),
                    )
                else:
                    connection.execute(
                        "UPDATE providers SET probe_lease_until=? WHERE provider_id=?",
                        ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), provider_id),
                    )
                connection.commit()
        expected.append(_facts(database, ignore_lease=change != "lease"))
        return [_attempt()]

    monkeypatch.setattr(health, "_perform_probe", transport)
    with pytest.raises(ApiError) as rejected:
        _call(catalog_subjects)
    assert rejected.value.code in {
        "provider_catalog.instance_changed", "provider_health.probe_superseded",
    }
    assert _facts(database) == expected[0]


@pytest.mark.anyio
@pytest.mark.parametrize(
    "fault", ["health", "outbox", "commit", "commit_auth", "network", "late_expiry"],
)
async def test_preflight_failure_rolls_back_all_facts_and_releases_only_own_lease(
    catalog_subjects, monkeypatch, fault,
):
    _app, database, _fixtures, _provider_id, _context = catalog_subjects
    opened = _track_connections(monkeypatch)

    def failure(*args, **kwargs):
        raise sqlite3.OperationalError("catalog injected failure")

    monkeypatch.setattr(health, "_perform_probe", lambda *a, **k: [_attempt()])
    if fault == "network":
        monkeypatch.setattr(health, "_perform_probe", failure)
    elif fault == "health":
        monkeypatch.setattr(health, "_update_credential_probe", failure)
    elif fault == "outbox":
        monkeypatch.setattr(service, "_catalog_outbox", failure)
    elif fault == "late_expiry":
        original_outbox = service._catalog_outbox

        def expire_after_outbox(*args, **kwargs):
            original_outbox(*args, **kwargs)
            future = datetime.now(UTC) + timedelta(days=1)
            monkeypatch.setattr(health, "_utcnow", lambda: future)

        monkeypatch.setattr(service, "_catalog_outbox", expire_after_outbox)
    else:
        original = ResourceWriteTransaction.commit
        commits = []

        def failing_second_commit(transaction, *args, **kwargs):
            commits.append(True)
            if len(commits) == 2:
                if fault == "commit":
                    return original(transaction, failure)
                workspace_access._revoke_workspace_session(
                    transaction.connection, transaction.context.session.session_id,
                    datetime.now(UTC), "authority_changed",
                )
            return original(transaction, *args, **kwargs)

        monkeypatch.setattr(ResourceWriteTransaction, "commit", failing_second_commit)
    before = _facts(database)
    with pytest.raises((ApiError, sqlite3.OperationalError)) as rejected:
        _call(catalog_subjects)
    if fault == "commit_auth":
        assert rejected.value.code == "workspace_session.revoked"
    elif fault == "late_expiry":
        assert rejected.value.code == "provider_health.probe_superseded"
    assert _facts(database) == before
    _assert_closed(opened)


@pytest.mark.anyio
@pytest.mark.parametrize("outcome", ["accepted", "rejected", "rate_limited"])
async def test_catalog_keypool_and_health_facts_commit_together(
    catalog_subjects, monkeypatch, outcome,
):
    app, database, _fixtures, provider_id, context = catalog_subjects
    plain_key = secrets.token_urlsafe(32)
    providers.create_provider_key(
        app.state.config, context, provider_id,
        provider_models.CreateProviderKeyRequest(name="Catalog test key", plain_value=plain_key),
        request_id="catalog-key", trace_id="catalog-key",
    )
    calls, deliveries = [], []

    def target(_config, base_url, **kwargs):
        assert base_url == "https://api.deepseek.com/v1"
        return base_url + "/models"

    def http_attempt(_config, _target, supplied_key, credential_id, **kwargs):
        assert hmac.compare_digest(supplied_key, plain_key)
        calls.append(True)
        status, code, http_status = {
            "accepted": ("success", None, 200),
            "rejected": ("failed", "authentication_failed", 401),
            "rate_limited": ("inconclusive", "rate_limited", 429),
        }[outcome]
        return health._Attempt(
            credential_id, status, outcome, http_status, 8, code,
            "Fixed Catalog test result.", datetime.now(UTC),
        )

    def delivered(*args, **kwargs):
        with closing(sqlite3.connect(database)) as connection:
            count = connection.execute("SELECT COUNT(*) FROM provider_preflight_runs").fetchone()[0]
            assert count == 1
        deliveries.append(True)

    monkeypatch.setattr(health, "_validated_probe_url", target)
    monkeypatch.setattr(health, "_http_attempt", http_attempt)
    monkeypatch.setattr(health, "_dispatch_unhealthy_event", delivered)
    result = _call(catalog_subjects)
    assert calls == [True] and deliveries == ([True] if outcome == "rejected" else [])
    assert plain_key not in result.model_dump_json()
    with closing(sqlite3.connect(database)) as connection:
        expected = {"accepted": "healthy", "rejected": "unhealthy", "rate_limited": "unknown"}
        assert connection.execute(
            "SELECT probe_health_status FROM provider_credentials WHERE provider_id=?",
            (provider_id,),
        ).fetchone()[0] == expected[outcome]
        count = connection.execute("SELECT COUNT(*) FROM provider_health_probe_logs").fetchone()[0]
        assert count == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM provider_catalog_events event "
            "JOIN provider_catalog_event_outbox outbox "
            "ON outbox.provider_catalog_event_id=event.provider_catalog_event_id "
            "WHERE event.event_type='provider.preflight_completed'"
        ).fetchone()[0] == 1


@pytest.mark.anyio
async def test_catalog_live_probe_lease_rejects_before_transport(catalog_subjects, monkeypatch):
    app, database, _fixtures, provider_id, context = catalog_subjects
    claim, _credentials = health._claim_provider(
        app.state.config, context.user.tenant_id, provider_id, "manual",
    )
    before = _facts(database)
    monkeypatch.setattr(health, "_perform_probe", lambda *a, **k: pytest.fail("unexpected network"))
    with pytest.raises(ApiError) as rejected:
        _call(catalog_subjects)
    assert rejected.value.code == "provider_health.probe_in_progress"
    assert _facts(database) == before
    health.release_provider_probe(app.state.config, claim)


@pytest.mark.anyio
async def test_catalog_rejects_mixed_owner_even_with_valid_token(catalog_subjects):
    _app, database, _fixtures, _provider_id, context = catalog_subjects
    mismatched = replace(context, user=context.user.model_copy(update={"user_id": "another-user"}))
    before = _facts(database)
    with pytest.raises(ApiError) as rejected:
        _call(catalog_subjects, "create", mismatched)
    assert rejected.value.code == "auth.permission_denied"
    assert _facts(database) == before


@pytest.mark.anyio
async def test_catalog_write_guards_and_current_viewer_deny(catalog_subjects, monkeypatch):
    _app, database, fixtures, provider_id, _context = catalog_subjects
    calls = []
    monkeypatch.setattr(health, "_perform_probe", lambda *a, **k: calls.append(True))
    owner_headers = _headers(fixtures.source, attack=True)
    before = _facts(database)
    for endpoint, payload in (
        ("/providers/from-catalog", _payload("catalog_denied").model_dump(mode="json")),
        (f"/providers/{provider_id}/preflight", {}),
    ):
        for headers, expected in (
            ({}, 401),
            ({**owner_headers, "X-CSRF-Token": "invalid"}, 403),
            ({**owner_headers, "Origin": "https://foreign.test"}, 403),
            ({**owner_headers, "Authorization": "Bearer invalid"}, 400),
        ):
            response = await fixtures.anonymous.post(
                endpoint, json=payload,
                headers={**headers, "Idempotency-Key": "catalog-denied-request"},
            )
            assert response.status_code == expected, response.json().get("code")
    assert not calls and _facts(database) == before
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
    before = _facts(database)
    for endpoint in ("/provider-catalog", "/provider-catalog/deepseek/compatibility"):
        response = await fixtures.member.get(endpoint)
        assert response.status_code == 403
    for endpoint, payload in (
        ("/providers/from-catalog", _payload("catalog_denied").model_dump(mode="json")),
        (f"/providers/{provider_id}/preflight", {}),
    ):
        response = await fixtures.member.post(
            endpoint, json=payload,
            headers={**_headers(fixtures.member), "Idempotency-Key": "catalog-viewer-denied"},
        )
        assert response.status_code == 403
    assert not calls and _facts(database) == before


@pytest.mark.anyio
async def test_catalog_create_replay_and_conflict(catalog_subjects):
    _app, database, _fixtures, _provider_id, _context = catalog_subjects
    created = _call(catalog_subjects, "create")
    before = _facts(database)
    replay = _call(catalog_subjects, "create")
    assert replay.idempotent_replay and replay.instance == created.instance
    assert _facts(database) == before
    # 浏览器后退后可能带着新的幂等键再次提交；同一组织、同一厂家仍应恢复原实例。
    same_entry_new_key = service.create_catalog_provider(
        _app.state.config,
        _context,
        _payload("catalog_third"),
        idempotency_key="catalog-transaction-third",
        request_id="catalog-third",
        trace_id="catalog-third",
    )
    assert same_entry_new_key.idempotent_replay and same_entry_new_key.instance == created.instance
    assert _facts(database) == before
    app, _database, _fixtures, _provider_id, context = catalog_subjects
    with pytest.raises(ApiError) as rejected:
        service.create_catalog_provider(
            app.state.config, context, _payload("changed_body"),
            idempotency_key="catalog-transaction-create",
            request_id="catalog-conflict", trace_id="catalog-conflict",
        )
    assert rejected.value.code == "provider_catalog.idempotency_conflict"
    assert _facts(database) == before


@pytest.mark.anyio
async def test_catalog_lease_covers_allowed_per_attempt_timeout(catalog_subjects, monkeypatch):
    app, _database, _fixtures, provider_id, context = catalog_subjects
    checked_at = datetime.now(UTC)

    def transport(config, claim, credentials):
        assert config.provider_health_timeout_seconds == 25
        assert claim.lease_until >= checked_at + timedelta(seconds=29)
        # Advance only the health clock; no wall-clock wait or session-auth substitution.
        monkeypatch.setattr(health, "_utcnow", lambda: checked_at + timedelta(seconds=24))
        return [_attempt()]

    monkeypatch.setattr(health, "_perform_probe", transport)
    result = service.preflight_catalog_provider(
        app.state.config, context, provider_id,
        models.ProviderPreflightRequest(timeout_seconds=25),
        idempotency_key="catalog-maximum-timeout",
        request_id="catalog-timeout", trace_id="catalog-timeout",
    )
    assert result.run.status == "succeeded"
