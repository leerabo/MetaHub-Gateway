"""Workspace feedback authorization at HTTP, transaction and replay boundaries."""

import threading
import time
from contextlib import closing
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from metahub_control.auth.resource_transactions import ResourceWriteTransaction
from metahub_control.feedback import routes as feedback_routes
from metahub_control.feedback import service as feedback_service
from metahub_control.models import routes as model_routes
from metahub_control.providers import routes as provider_routes
from metahub_control.workspace_access import service as workspace_access
from metahub_shared.db.connection import connect_database, database_request_scope
from metahub_shared.feedback import normalize_feedback_payload, storage
from metahub_shared.runtime.errors import ApiError
from test_prd147_workspace_files import (  # noqa: F401
    WORKSPACE_SESSION,
    _headers,
    file_subjects,
    migrated_baseline,
)
from test_prd147_workspace_logs import _legacy


@pytest.fixture
async def feedback_subjects(file_subjects):  # noqa: F811
    app, database, fixtures = file_subjects
    from metahub_control.app import response_cache_private_cache_boundary

    app.middleware_stack = None
    app.middleware("http")(response_cache_private_cache_boundary)
    for router in (feedback_routes.router, provider_routes.router, model_routes.router):
        app.include_router(router)
    created = await fixtures.source.post(
        "/providers",
        headers=_headers(fixtures.source),
        json={
            "provider_code": "feedback_test",
            "name": "Feedback test",
            "base_url": "https://feedback.example.test/v1",
        },
    )
    assert created.status_code == 201, created.json().get("code")
    provider = created.json()["provider"]
    created = await fixtures.source.post(
        "/models",
        headers=_headers(fixtures.source),
        json={
            "internal_model_id": "feedback-test",
            "provider_id": provider["provider_id"],
            "provider_model_id": "feedback-test",
            "capabilities": {"chat": True},
        },
    )
    assert created.status_code == 201, created.json().get("code")
    model = created.json()["model"]
    ids = ["req_feedback_identity_" + uuid4().hex for _ in range(3)]
    with closing(connect_database(app.state.config.database_url)) as connection:
        timestamp = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        for identifier in ids:
            connection.execute(
                "INSERT INTO gateway_request_claims(tenant_id,request_id,trace_id,virtual_key_id,"
                "snapshot_version,policy_snapshot_status,claimed_at) "
                "VALUES(?,?,?,NULL,NULL,'disabled',?)",
                (provider["tenant_id"], identifier, identifier, timestamp),
            )
            connection.execute(
                "INSERT INTO request_logs(request_id,tenant_id,trace_id,status,"
                "provider_id,model_id,"
                "request_model,resolved_model,provider_code,created_at,updated_at) "
                "VALUES(?,?,?,'success',?,?,'feedback-test','feedback-test','feedback_test',?,?)",
                (
                    identifier,
                    provider["tenant_id"],
                    identifier,
                    provider["provider_id"],
                    model["model_id"],
                    timestamp,
                    timestamp,
                ),
            )
        connection.commit()
    return app, database, fixtures, ids


def _payload(identifier, rating=5):
    return {"request_id": identifier, "rating": rating, "accepted": True, "tags": ["helpful"]}


def _facts(config):
    with closing(connect_database(config.database_url)) as connection:
        return {
            table: [tuple(row) for row in connection.execute(f"SELECT * FROM {table}")]
            for table in (
                "request_feedbacks",
                "request_feedback_events",
                "model_feedback_statistics",
            )
        }


@pytest.mark.anyio
async def test_workspace_feedback_create_replay_scope_and_summary(feedback_subjects):
    app, _database, f, ids = feedback_subjects
    headers = {**_headers(f.source), "X-Request-ID": "feedback-workspace-operation"}
    for invalid in (
        {},
        {**headers, "X-CSRF-Token": "invalid"},
        {**headers, "Origin": "https://foreign.test"},
    ):
        response = await f.source.post("/feedback", headers=invalid, json=_payload(ids[0]))
        assert response.status_code == 403, response.json().get("code")
    response = await f.source.post("/feedback", headers=headers, json=_payload(ids[0]))
    assert response.status_code == 201, response.json().get("code")
    identity = response.json()["feedback"]["request_feedback_id"]
    response = await f.source.post("/feedback", headers=headers, json=_payload(ids[0]))
    assert response.status_code == 200 and response.json()["idempotent_replay"]
    assert response.json()["feedback"]["request_feedback_id"] == identity
    response = await f.source.post("/feedback", headers=headers, json=_payload(ids[0], 4))
    assert response.status_code == 409
    foreign = await f.target.post("/feedback", headers=_headers(f.target), json=_payload(ids[0]))
    missing = await f.target.post("/feedback", headers=_headers(f.target), json=_payload("missing"))
    assert foreign.status_code == missing.status_code == 404
    assert foreign.json()["code"] == missing.json()["code"]
    response = await f.source.get("/feedback/summary")
    assert response.status_code == 200, response.json().get("code")
    assert response.json()["summary"]["feedback_count"] == 1
    assert response.json()["summary"]["average_rating_milli"] == 5000
    assert response.headers["cache-control"] == "no-store, private"
    assert (await f.target.get("/feedback/summary")).json()["summary"]["feedback_count"] == 0
    legacy_headers, _ = await _legacy(app, f)
    response = await f.anonymous.post(
        "/feedback",
        headers=legacy_headers,
        json=_payload(ids[1]),
    )
    assert response.status_code == 201, response.json().get("code")
    mixed = await f.source.get("/feedback/summary", headers=legacy_headers)
    assert mixed.status_code == 400


@pytest.mark.anyio
async def test_feedback_commit_replay_and_conflict_recheck_expiry(feedback_subjects, monkeypatch):
    app, _database, f, ids = feedback_subjects
    context = workspace_access.authenticate_workspace_session_as_auth_context(
        app.state.config,
        f.source.cookies.get(WORKSPACE_SESSION),
    )
    feedback_service.create_session_feedback(
        app.state.config,
        context,
        operation_request_id="existing-feedback",
        trace_id="feedback-trace",
        intent=normalize_feedback_payload(_payload(ids[0])),
    )
    original = ResourceWriteTransaction.verify
    future = workspace_access._utcnow() + timedelta(days=2)
    seen = set()

    def expire_at_terminal(transaction):
        if transaction.connection in seen:
            with monkeypatch.context() as patch:
                patch.setattr(workspace_access, "_utcnow", lambda: future)
                return original(transaction)
        seen.add(transaction.connection)
        return original(transaction)

    before = _facts(app.state.config)
    with monkeypatch.context() as patch:
        patch.setattr(ResourceWriteTransaction, "verify", expire_at_terminal)
        for identifier, operation, rating in (
            (ids[1], "new-feedback", 5),
            (ids[0], "existing-feedback", 5),
            (ids[0], "existing-feedback", 4),
            (ids[0], "different-feedback", 5),
            ("missing", "missing-feedback", 5),
        ):
            seen.clear()
            with pytest.raises(ApiError) as error:
                feedback_service.create_session_feedback(
                    app.state.config,
                    context,
                    operation_request_id=operation,
                    trace_id="feedback-trace",
                    intent=normalize_feedback_payload(_payload(identifier, rating)),
                )
            assert error.value.code == "workspace_session.expired"
            assert _facts(app.state.config) == before


@pytest.mark.anyio
async def test_feedback_summary_checks_new_snapshot_revocation(feedback_subjects, monkeypatch):
    app, _database, f, _ids = feedback_subjects
    context = workspace_access.authenticate_workspace_session_as_auth_context(
        app.state.config,
        f.source.cookies.get(WORKSPACE_SESSION),
    )
    original = storage._summary_result

    with closing(connect_database(app.state.config.database_url)) as revoker:
        revoker.execute("SELECT 1 FROM workspace_sessions LIMIT 1").fetchone()

        def revoke_after_snapshot(**kwargs):
            revoker.execute(
                "UPDATE workspace_sessions SET status='revoked',revoked_at=?,"
                "revoked_reason='logout' "
                "WHERE workspace_session_id=?",
                (datetime.now(UTC).isoformat().replace("+00:00", "Z"), context.session.session_id),
            )
            revoker.commit()
            return original(**kwargs)

        monkeypatch.setattr(storage, "_summary_result", revoke_after_snapshot)
        with database_request_scope(reuse_connections=True):
            workspace_access.authenticate_workspace_session_as_auth_context(
                app.state.config,
                f.source.cookies.get(WORKSPACE_SESSION),
            )
            with pytest.raises(ApiError) as error:
                feedback_service.summarize_session_feedback(
                    app.state.config,
                    context,
                    model_id=None,
                    from_time=datetime.now(UTC).replace(microsecond=0) - timedelta(days=1),
                    to_time=datetime.now(UTC).replace(microsecond=0),
                    limit=20,
                    offset=0,
                    request_id="feedback-summary",
                    trace_id="feedback-summary",
                    monotonic_deadline=time.monotonic() + 0.250,
                )
    if error.value.code != "workspace_session.revoked":
        raise error.value


@pytest.mark.anyio
async def test_workspace_feedback_authentication_retains_deadline(feedback_subjects):
    app, _database, f, _ids = feedback_subjects
    config = app.state.config
    token = f.source.cookies.get(WORKSPACE_SESSION)
    if config.database_url.startswith("postgres"):
        pytest.skip("SQLite writer contention contract")
    with closing(connect_database(config.database_url)) as writer:
        writer.execute("BEGIN IMMEDIATE")
        started = time.monotonic()
        with pytest.raises(ApiError) as error:
            workspace_access.authenticate_workspace_session_as_auth_context(
                config,
                token,
                monotonic_deadline=started + 0.060,
                deadline_error_code="feedback.unavailable",
            )
        assert error.value.code == "feedback.unavailable"
        assert time.monotonic() - started < 0.250
        writer.rollback()
    assert workspace_access.authenticate_workspace_session_as_auth_context(config, token)


@pytest.mark.anyio
async def test_feedback_write_does_not_block_event_loop(feedback_subjects, monkeypatch):
    _app, _database, fixtures, ids = feedback_subjects
    loop_thread = threading.get_ident()
    original = feedback_routes.create_session_feedback
    worker_threads = []

    def create_on_worker(*args, **kwargs):
        worker_threads.append(threading.get_ident())
        assert worker_threads[-1] != loop_thread
        return original(*args, **kwargs)

    monkeypatch.setattr(feedback_routes, "create_session_feedback", create_on_worker)
    response = await fixtures.source.post(
        "/feedback", headers=_headers(fixtures.source), json=_payload(ids[0]),
    )
    assert response.status_code == 201, response.json().get("code")
    assert len(worker_threads) == 1
