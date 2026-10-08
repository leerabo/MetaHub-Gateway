"""Real Cookie resource writes and stale-session rejection; not full G7 acceptance."""

import asyncio
import hashlib
import json
import secrets
import sqlite3
import threading
from contextlib import closing
from dataclasses import replace
from datetime import timedelta
from functools import partial

import pytest
from metahub_control.auth.resource_transactions import ResourceWriteTransaction
from metahub_control.environments import models as environment_models
from metahub_control.environments import routes as environment_routes
from metahub_control.environments import service as environments
from metahub_control.models import models as model_models
from metahub_control.models import routes as model_routes
from metahub_control.models import service as models
from metahub_control.projects import models as project_models
from metahub_control.projects import service as projects
from metahub_control.providers import models as provider_models
from metahub_control.providers import routes as provider_routes
from metahub_control.providers import service as providers
from metahub_control.virtual_keys import models as key_models
from metahub_control.virtual_keys import routes as key_routes
from metahub_control.virtual_keys import service as keys
from metahub_control.workspace_access import service as workspace_access
from metahub_control.workspace_access.service import authenticate_workspace_session_as_auth_context
from metahub_shared.db import connect_database
from metahub_shared.runtime.errors import ApiError
from test_prd147_workspace_files import (  # noqa: F401
    WORKSPACE_SESSION,
    _headers,
    file_subjects,
    migrated_baseline,
)

TABLES = (
    "projects",
    "applications",
    "environments",
    "providers",
    "provider_credentials",
    "models",
    "virtual_keys",
    "virtual_key_rate_limit_rules",
    "project_events",
    "environment_events",
    "audit_logs",
)


def _facts(database):
    with closing(sqlite3.connect(database)) as connection:
        return {
            table: hashlib.sha256(
                json.dumps(
                    sorted(connection.execute(f'SELECT * FROM "{table}"').fetchall(), key=repr),
                    default=str,
                ).encode()
            ).hexdigest()
            for table in TABLES
        }


@pytest.mark.anyio
async def test_policy_template_apply_rechecks_workspace_session_in_write_transaction(file_subjects):  # noqa: F811
    app, database, f = file_subjects
    current = authenticate_workspace_session_as_auth_context(
        app.state.config, f.source.cookies.get(WORKSPACE_SESSION)
    )
    assert "policy_template.apply" in current.permission_codes
    connection = connect_database(f"sqlite:///{database.as_posix()}")
    try:
        connection.execute("BEGIN IMMEDIATE")
        verified = ResourceWriteTransaction(
            connection, app.state.config, current, "policy_template.apply"
        ).verify()
        assert verified.user.user_id == current.user.user_id
    finally:
        connection.rollback()
        connection.close()


@pytest.fixture
async def resource_subjects(file_subjects):  # noqa: F811
    app, database, f = file_subjects
    for router in (
        environment_routes.router,
        provider_routes.router,
        provider_routes.provider_keys_router,
        model_routes.router,
        key_routes.router,
    ):
        app.include_router(router)

    async def create(path, payload, field):
        response = await f.source.post(path, json=payload, headers=_headers(f.source))
        assert response.status_code == 201, response.json().get("code")
        return response.json()[field]

    environment = await create("/environments", {"code": "test", "name": "Test"}, "environment")
    application = await create(
        "/applications",
        {
            "project_id": f.source_project_id,
            "application_code": "resource-app",
            "name": "Test",
        },
        "application",
    )
    provider = await create(
        "/providers",
        {
            "provider_code": "resource_provider",
            "name": "Test",
            "base_url": "https://provider.test/v1",
        },
        "provider",
    )
    credential = await create(
        f"/providers/{provider['provider_id']}/keys",
        {
            "name": "Test",
            "plain_value": secrets.token_urlsafe(32),
        },
        "provider_key",
    )
    model = await create(
        "/models",
        {
            "internal_model_id": "resource-model",
            "provider_id": provider["provider_id"],
            "provider_model_id": "resource-model",
            "capabilities": {"chat": True},
        },
        "model",
    )
    key = await create(
        "/virtual-keys",
        {
            "name": "Test",
            "environment_id": environment["environment_id"],
        },
        "virtual_key",
    )
    return (
        app,
        database,
        f,
        {
            "environment": environment["environment_id"],
            "application": application["application_id"],
            "provider": provider["provider_id"],
            "credential": credential["provider_key_id"],
            "model": model["model_id"],
            "key": key["virtual_key_id"],
        },
    )


def _operations(config, context, ids, project_id):
    def call(function, *args):
        return partial(
            function,
            config,
            context,
            *args,
            request_id="resource-write-test",
            trace_id="resource-write-test",
        )

    return {
        "project.create": call(
            projects.create_project,
            project_models.CreateProjectRequest(project_code="after-auth", name="After auth"),
        ),
        "project.update": call(
            projects.update_project, project_id, project_models.UpdateProjectRequest(name="Updated")
        ),
        "application.create": call(
            projects.create_application,
            project_models.CreateApplicationRequest(
                project_id=project_id, application_code="after-auth", name="After auth"
            ),
        ),
        "application.update": call(
            projects.update_application,
            ids["application"],
            project_models.UpdateApplicationRequest(name="Updated"),
        ),
        "environment.create": call(
            environments.create_environment,
            environment_models.CreateEnvironmentRequest(code="prod", name="After auth"),
        ),
        "environment.update": call(
            environments.update_environment,
            ids["environment"],
            environment_models.UpdateEnvironmentRequest(name="Updated"),
        ),
        "provider.create": call(
            providers.create_provider,
            provider_models.CreateProviderRequest(
                provider_code="after_auth", name="After auth", base_url="https://provider.test/v1"
            ),
        ),
        "provider.update": call(
            providers.update_provider,
            ids["provider"],
            provider_models.UpdateProviderRequest(name="Updated"),
        ),
        "credential.create": call(
            providers.create_provider_key,
            ids["provider"],
            provider_models.CreateProviderKeyRequest(
                name="After auth", plain_value=secrets.token_urlsafe(32)
            ),
        ),
        "credential.update": call(
            providers.update_provider_key,
            ids["credential"],
            provider_models.UpdateProviderKeyRequest(name="Updated"),
        ),
        "model.create": call(
            models.create_model,
            model_models.CreateModelRequest(
                internal_model_id="after-auth",
                provider_id=ids["provider"],
                provider_model_id="after-auth",
                capabilities={"chat": True},
            ),
        ),
        "model.update": call(
            models.update_model,
            ids["model"],
            model_models.UpdateModelRequest(display_name="Updated"),
        ),
        "key.create": call(
            keys.create_virtual_key,
            key_models.CreateVirtualKeyRequest(
                name="After auth", environment_id=ids["environment"]
            ),
        ),
        "key.update": call(
            keys.update_virtual_key, ids["key"], key_models.UpdateVirtualKeyRequest(name="Updated")
        ),
        "key.limits": call(
            keys.update_virtual_key_limits,
            ids["key"],
            key_models.UpdateVirtualKeyLimitsRequest(rpm_limit=50),
        ),
        "key.disable": call(keys.disable_virtual_key, ids["key"]),
    }


@pytest.mark.anyio
async def test_real_resource_writes_reject_logged_out_context(resource_subjects, record_property):
    app, database, f, ids = resource_subjects
    stale = authenticate_workspace_session_as_auth_context(
        app.state.config,
        f.source.cookies.get(WORKSPACE_SESSION),
    )
    operations = _operations(app.state.config, stale, ids, f.source_project_id)
    logout = await f.source.post(
        "/identity/logout", headers=_headers(f.source, global_session=True)
    )
    assert logout.status_code == 200
    before = _facts(database)
    outcomes = {}
    for name, operation in operations.items():
        try:
            operation()
        except ApiError as error:
            outcomes[name] = error.code
        else:
            outcomes[name] = "unexpected_commit"
    record_property("resource_write_outcomes", json.dumps(outcomes, sort_keys=True))
    assert all(code == "workspace_session.revoked" for code in outcomes.values()), outcomes
    assert _facts(database) == before


@pytest.mark.anyio
async def test_real_resource_writes_accept_current_cookie_context(
    resource_subjects, record_property
):
    app, database, f, ids = resource_subjects
    current = authenticate_workspace_session_as_auth_context(
        app.state.config,
        f.source.cookies.get(WORKSPACE_SESSION),
    )
    operations = _operations(app.state.config, current, ids, f.source_project_id)
    for operation in operations.values():
        result = operation()
        assert result.request_id == "resource-write-test"
    record_property("accepted_operations", json.dumps(list(operations)))
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute(
            "SELECT status FROM virtual_keys WHERE virtual_key_id=?", (ids["key"],)
        ).fetchone() == ("disabled",)


@pytest.mark.anyio
@pytest.mark.parametrize("failure", ["expiry", "commit"])
async def test_resource_write_failures_roll_back_and_close(
    resource_subjects, monkeypatch, failure, record_property
):
    app, database, f, ids = resource_subjects
    current = authenticate_workspace_session_as_auth_context(
        app.state.config,
        f.source.cookies.get(WORKSPACE_SESSION),
    )
    operations = _operations(app.state.config, current, ids, f.source_project_id)
    opened = []
    for service in (projects, environments, providers, models, keys):
        original_connect = service._connect

        def tracked_connect(config, original=original_connect):
            connection = original(config)
            opened.append(connection)
            return connection

        monkeypatch.setattr(service, "_connect", tracked_connect)
    original_commit = ResourceWriteTransaction.commit
    future = workspace_access._utcnow() + timedelta(days=2)

    def failed_committer(_connection):
        raise sqlite3.OperationalError("test commit failure")

    def fail_at_commit(transaction, committer=None):
        if failure == "commit":
            return original_commit(transaction, failed_committer)
        with monkeypatch.context() as clock:
            clock.setattr(workspace_access, "_utcnow", lambda: future)
            return original_commit(transaction, committer)

    monkeypatch.setattr(ResourceWriteTransaction, "commit", fail_at_commit)
    before = _facts(database)
    for operation in operations.values():
        if failure == "expiry":
            with pytest.raises(ApiError) as rejected:
                operation()
            assert rejected.value.code == "workspace_session.expired"
        else:
            with pytest.raises(sqlite3.OperationalError, match="test commit failure"):
                operation()
        assert _facts(database) == before
    assert len(opened) == 16
    for connection in opened:
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            connection.execute("SELECT 1")
    record_property("rolled_back_operations", json.dumps(list(operations)))


@pytest.mark.anyio
@pytest.mark.parametrize("change", ["revoked", "binding_disabled", "none"])
async def test_key_collision_restarts_transaction_and_rechecks_revocation(
    resource_subjects, monkeypatch, change
):
    app, database, f, ids = resource_subjects
    current = authenticate_workspace_session_as_auth_context(
        app.state.config,
        f.source.cookies.get(WORKSPACE_SESSION),
    )
    request = key_models.CreateVirtualKeyRequest(
        name="Collision test", environment_id=ids["environment"]
    )
    existing = keys.create_virtual_key(
        app.state.config,
        current,
        request,
        request_id="resource-collision",
        trace_id="resource-collision",
    )
    before = _facts(database)
    original_begin = ResourceWriteTransaction.begin
    attempts = 0

    def revoke_before_retry(transaction):
        nonlocal attempts
        attempts += 1
        if attempts == 2:
            assert not transaction.connection.in_transaction
            with closing(sqlite3.connect(database)) as connection:
                if change == "revoked":
                    workspace_access._revoke_workspace_session(
                        connection,
                        current.session.session_id,
                        workspace_access._utcnow(),
                        "logout",
                    )
                elif change == "binding_disabled":
                    connection.execute(
                        "UPDATE environments SET status='disabled' WHERE environment_id=?",
                        (ids["environment"],),
                    )
                connection.commit()
            before.update(_facts(database))
        original_begin(transaction)

    monkeypatch.setattr(ResourceWriteTransaction, "begin", revoke_before_retry)
    candidates = iter((existing.plain_value, secrets.token_urlsafe(32)))
    monkeypatch.setattr(keys, "_generate_virtual_key", lambda _environment: next(candidates))
    if change == "none":
        created = keys.create_virtual_key(
            app.state.config,
            current,
            request,
            request_id="resource-collision",
            trace_id="resource-collision",
        )
        assert created.virtual_key.virtual_key_id != existing.virtual_key.virtual_key_id
    else:
        with pytest.raises(ApiError) as rejected:
            keys.create_virtual_key(
                app.state.config,
                current,
                request,
                request_id="resource-collision",
                trace_id="resource-collision",
            )
        assert rejected.value.code == (
            "workspace_session.revoked" if change == "revoked" else "environment.disabled"
        )
        assert _facts(database) == before
    assert attempts == 2


@pytest.mark.anyio
async def test_cookie_http_resource_mutations_require_single_domain_and_csrf(
    resource_subjects, record_property
):
    app, _database, f, ids = resource_subjects
    current = authenticate_workspace_session_as_auth_context(
        app.state.config,
        f.source.cookies.get(WORKSPACE_SESSION),
    )
    routes = {
        "project.create": ("POST", "/projects"),
        "project.update": ("PATCH", f"/projects/{f.source_project_id}"),
        "application.create": ("POST", "/applications"),
        "application.update": ("PATCH", f"/applications/{ids['application']}"),
        "environment.create": ("POST", "/environments"),
        "environment.update": ("PATCH", f"/environments/{ids['environment']}"),
        "provider.create": ("POST", "/providers"),
        "provider.update": ("PATCH", f"/providers/{ids['provider']}"),
        "credential.create": ("POST", f"/providers/{ids['provider']}/keys"),
        "credential.update": ("PATCH", f"/provider-keys/{ids['credential']}"),
        "model.create": ("POST", "/models"),
        "model.update": ("PATCH", f"/models/{ids['model']}"),
        "key.create": ("POST", "/virtual-keys"),
        "key.update": ("PATCH", f"/virtual-keys/{ids['key']}"),
        "key.limits": ("PATCH", f"/virtual-keys/{ids['key']}/limits"),
        "key.disable": ("DELETE", f"/virtual-keys/{ids['key']}"),
    }
    operations = _operations(app.state.config, current, ids, f.source_project_id)
    assert set(routes) == set(operations)
    for name, operation in operations.items():
        method, path = routes[name]
        payload = operation.args[-1]
        body = (
            {"json": payload.model_dump(exclude_unset=True)}
            if hasattr(payload, "model_dump")
            else {}
        )
        anonymous = await f.anonymous.request(method, path, **body)
        assert anonymous.status_code == 401, name
        headers = _headers(f.source)
        csrf = await f.source.request(
            method, path, headers={**headers, "X-CSRF-Token": "invalid"}, **body
        )
        assert csrf.status_code == 403, name
        mixed = await f.source.request(
            method, path, headers={**headers, "Authorization": "Bearer invalid"}, **body
        )
        assert mixed.status_code == 400, name
        response = await f.source.request(method, path, headers=headers, **body)
        assert response.status_code == (201 if method == "POST" else 200), (
            name,
            response.json().get("code"),
        )
        owner = next(
            value
            for value in response.json().values()
            if isinstance(value, dict) and "tenant_id" in value
        )
        assert owner["tenant_id"] == f.source_tenant_id
    record_property("http_resource_operations", json.dumps(list(operations)))


@pytest.mark.anyio
async def test_resource_write_owner_cannot_mix_two_real_sessions(resource_subjects):
    app, database, f, _ids = resource_subjects
    current, other = [
        authenticate_workspace_session_as_auth_context(
            app.state.config,
            client.cookies.get(WORKSPACE_SESSION),
        )
        for client in (f.source, f.target)
    ]
    before = _facts(database)
    for field in ("user", "tenant", "session", "token_jti", "token"):
        mixed = replace(current, **{field: getattr(other, field)})
        with pytest.raises(ApiError) as rejected:
            projects.create_project(
                app.state.config,
                mixed,
                project_models.CreateProjectRequest(project_code="mixed_owner", name="Test"),
                request_id="resource-owner-test",
                trace_id="resource-owner-test",
            )
        assert rejected.value.code == "auth.permission_denied"
    assert _facts(database) == before


@pytest.mark.anyio
async def test_sqlite_resource_write_serializes_competing_revocation(file_subjects, monkeypatch):  # noqa: F811
    app, database, f = file_subjects
    current = authenticate_workspace_session_as_auth_context(
        app.state.config,
        f.source.cookies.get(WORKSPACE_SESSION),
    )
    entered, release = threading.Event(), threading.Event()
    original_load = projects._load_project

    def paused_load(connection, tenant_id, project_id):
        if not entered.is_set():
            assert connection.in_transaction
            entered.set()
            if not release.wait(15):
                raise RuntimeError("resource write barrier timed out")
        return original_load(connection, tenant_id, project_id)

    monkeypatch.setattr(projects, "_load_project", paused_load)
    payload = project_models.UpdateProjectRequest(name="Serialized write")
    writer = asyncio.create_task(
        asyncio.to_thread(
            projects.update_project,
            app.state.config,
            current,
            f.source_project_id,
            payload,
            request_id="resource-serialization",
            trace_id="resource-serialization",
        )
    )
    try:
        async with asyncio.timeout(10):
            while not entered.is_set():
                if writer.done():
                    await writer
                    pytest.fail("resource write did not reach barrier")
                await asyncio.sleep(0.01)
        with closing(sqlite3.connect(database, timeout=0)) as contender:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                workspace_access._revoke_workspace_session(
                    contender,
                    current.session.session_id,
                    workspace_access._utcnow(),
                    "logout",
                )
            contender.rollback()
    finally:
        release.set()
        await asyncio.wait_for(writer, 20)
    logout = await f.source.post(
        "/identity/logout", headers=_headers(f.source, global_session=True)
    )
    assert logout.status_code == 200
    with pytest.raises(ApiError) as rejected:
        projects.update_project(
            app.state.config,
            current,
            f.source_project_id,
            payload,
            request_id="resource-serialization",
            trace_id="resource-serialization",
        )
    assert rejected.value.code == "workspace_session.revoked"
