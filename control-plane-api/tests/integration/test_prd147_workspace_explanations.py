"""Real session ownership and terminal authorization for explanation access facts."""

import sqlite3
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from metahub_control.auth.resource_transactions import ResourceWriteTransaction
from metahub_control.request_explanations import outbox, routes, service
from metahub_control.workspace_access import service as workspace_access
from metahub_shared.db import apply_migrations, rollback_last_migration
from metahub_shared.db.connection import connect_database, database_request_scope
from metahub_shared.db.errors import DbError
from metahub_shared.runtime.errors import ApiError
from test_prd147_workspace_feedback import feedback_subjects  # noqa: F401
from test_prd147_workspace_files import (  # noqa: F401
    WORKSPACE_SESSION,
    _headers,
    file_subjects,
    migrated_baseline,
)
from test_prd147_workspace_logs import _legacy


@pytest.fixture
async def explanation_subjects(feedback_subjects):  # noqa: F811
    app, database, fixtures, identifiers = feedback_subjects
    app.include_router(routes.router)
    return app, database, fixtures, identifiers


def _facts(config):
    with closing(connect_database(config.database_url)) as connection:
        prefix = "" if config.database_url.startswith("postgres") else "rowid,"
        return [
            tuple(row)
            for row in connection.execute(
                f"SELECT {prefix}* FROM request_explanations ORDER BY request_explanation_id"
            )
        ]


@pytest.mark.anyio
async def test_explanation_projection_uses_commit_permission_snapshot(
    explanation_subjects, monkeypatch
):
    app, _, f, ids = explanation_subjects
    original = ResourceWriteTransaction.verify
    seen = set()

    def final_permissions(transaction):
        context = original(transaction)
        if not transaction.read_only and transaction.connection in seen:
            context = replace(
                context,
                user=context.user.model_copy(
                    update={
                        "permission_codes": ("request_explanation.read",),
                    }
                ),
            )
            transaction.context = context
        seen.add(transaction.connection)
        return context

    monkeypatch.setattr(ResourceWriteTransaction, "verify", final_permissions)
    result = await f.source.get(f"/request-explain/{ids[0]}", headers=_headers(f.source))
    assert result.status_code == 200, result.json().get("code")
    explanation = result.json()["request_explanation"]
    assert explanation["status"] == "restricted"
    assert explanation["summary"]["provider_code"] is None
    assert explanation["summary"]["input_tokens"] is None
    assert explanation["authentication"]["facts"] == []
    with closing(connect_database(app.state.config.database_url)) as connection:
        row = connection.execute(
            "SELECT status,section_status_json FROM request_explanations"
        ).fetchone()
        assert row["status"] in {"complete", "partial"}
        assert "restricted" not in row["section_status_json"]


@pytest.mark.anyio
async def test_workspace_explanation_http_scope_and_real_owner(explanation_subjects):
    app, _, f, ids = explanation_subjects
    config = app.state.config
    path = f"/request-explain/{ids[0]}"
    for invalid in (
        {},
        {**_headers(f.source), "X-CSRF-Token": "invalid"},
        {**_headers(f.source), "Origin": "https://foreign.test"},
    ):
        denied = await f.source.get(path, headers=invalid)
        assert denied.status_code == 403, denied.json().get("code")
    assert not _facts(config)
    response = await f.source.get(path, headers=_headers(f.source))
    assert response.status_code == 200, response.json().get("code")
    assert response.json()["request_explanation"]["source_request_id"] == ids[0]
    assert response.headers["cache-control"] == "no-store, private"
    assert {part.strip().lower() for part in response.headers["vary"].split(",")} >= {
        "authorization",
        "cookie",
    }
    context = workspace_access.authenticate_workspace_session_as_auth_context(
        config,
        f.source.cookies.get(WORKSPACE_SESSION),
    )
    with closing(connect_database(config.database_url)) as connection:
        row = connection.execute("SELECT * FROM request_explanations").fetchone()
        assert row["workspace_session_id"] == context.session.session_id
        assert row["actor_session_id"] is None
        assert row["actor_user_id"] == context.user.user_id
    foreign = await f.target.get(path, headers=_headers(f.target))
    missing = await f.target.get("/request-explain/missing", headers=_headers(f.target))
    assert foreign.status_code == missing.status_code == 404
    assert foreign.json()["code"] == missing.json()["code"]
    legacy_headers, _ = await _legacy(app, f)
    mixed = await f.source.get(path, headers=legacy_headers)
    assert mixed.status_code == 400
    legacy = await f.anonymous.get(path, headers=legacy_headers)
    assert legacy.status_code == 200, legacy.json().get("code")
    with closing(connect_database(config.database_url)) as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM request_explanations "
                "WHERE actor_session_id IS NOT NULL "
                "AND workspace_session_id IS NULL"
            ).fetchone()[0]
            == 1
        )


@pytest.mark.anyio
async def test_explanation_terminal_expiry_leaves_no_access_fact(explanation_subjects, monkeypatch):
    app, _, f, ids = explanation_subjects
    config = app.state.config
    context = workspace_access.authenticate_workspace_session_as_auth_context(
        config,
        f.source.cookies.get(WORKSPACE_SESSION),
    )
    original = ResourceWriteTransaction.verify
    future = workspace_access._utcnow() + timedelta(days=2)
    for read_only, identifier in ((True, ids[0]), (True, "missing"), (False, ids[0])):
        seen = set()

        def expire_at_terminal(transaction, read_only=read_only, seen=seen):
            if transaction.read_only == read_only and transaction.connection in seen:
                with monkeypatch.context() as patch:
                    patch.setattr(workspace_access, "_utcnow", lambda: future)
                    return original(transaction)
            seen.add(transaction.connection)
            return original(transaction)

        with monkeypatch.context() as patch:
            patch.setattr(ResourceWriteTransaction, "verify", expire_at_terminal)
            with pytest.raises(ApiError) as error:
                service.get_request_explanation(
                    config, context, identifier, request_id="test-operation", trace_id="test-trace"
                )
            assert error.value.code == "workspace_session.expired"
        assert not _facts(config)


@pytest.mark.anyio
async def test_explanation_read_rechecks_revocation_outside_snapshot(
    explanation_subjects, monkeypatch
):
    app, _, f, ids = explanation_subjects
    config = app.state.config
    context = workspace_access.authenticate_workspace_session_as_auth_context(
        config,
        f.source.cookies.get(WORKSPACE_SESSION),
    )
    original = ResourceWriteTransaction.begin
    seen = set()
    with closing(connect_database(config.database_url)) as revoker:
        revoker.execute("SELECT 1 FROM workspace_sessions LIMIT 1").fetchone()

        def revoke_after_read(transaction):
            if transaction.read_only and transaction.connection in seen:
                assert not transaction.connection.in_transaction
                revoker.execute(
                    "UPDATE workspace_sessions SET status='revoked',revoked_at=?,"
                    "revoked_reason='logout' WHERE workspace_session_id=?",
                    (
                        datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                        context.session.session_id,
                    ),
                )
                revoker.commit()
            seen.add(transaction.connection)
            return original(transaction)

        monkeypatch.setattr(ResourceWriteTransaction, "begin", revoke_after_read)
        with database_request_scope(reuse_connections=True), pytest.raises(ApiError) as error:
            service.get_request_explanation(
                config, context, ids[0], request_id="test-operation", trace_id="test-trace"
            )
        if error.value.code != "workspace_session.revoked":
            raise error.value
    assert not _facts(config)


@pytest.mark.anyio
async def test_explanation_owner_xor_outbox_and_lossless_down_guard(explanation_subjects):
    app, _, f, ids = explanation_subjects
    config = app.state.config
    response = await f.source.get(f"/request-explain/{ids[0]}", headers=_headers(f.source))
    assert response.status_code == 200, response.json().get("code")
    target = workspace_access.authenticate_workspace_session_as_auth_context(
        config,
        f.target.cookies.get(WORKSPACE_SESSION),
    )
    with closing(connect_database(config.database_url)) as connection:
        row = dict(connection.execute("SELECT * FROM request_explanations").fetchone())
        for changes in (
            {"workspace_session_id": None},
            {"workspace_session_id": target.session.session_id},
            {"actor_user_id": target.user.user_id},
            {"actor_session_id": target.session.session_id},
        ):
            values = {
                **row,
                **changes,
                "request_explanation_id": "rxe_" + uuid4().hex,
                "event_id": "rxevt_" + uuid4().hex,
            }
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(
                    "INSERT INTO request_explanations("
                    + ",".join(values)
                    + ") VALUES("
                    + ",".join("?" for _ in values)
                    + ")",
                    tuple(values.values()),
                )
                connection.commit()
            connection.rollback()
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE request_explanations SET workspace_session_id=?",
                (target.session.session_id,),
            )
        connection.rollback()
    claim = outbox.claim_next_request_explanation_event(config, worker_id="test-explanation-worker")
    assert claim is not None
    assert outbox.dispatch_claimed_request_explanation_event(config, claim)
    before = _facts(config)
    version = "0148" if config.database_url.startswith("postgres") else "0145"
    with pytest.raises(DbError):
        rollback_last_migration(config.database_url, expected_version=version)
    assert _facts(config) == before


@pytest.mark.anyio
async def test_explanation_legacy_history_roundtrip_and_drift_guard(explanation_subjects):
    app, _, f, ids = explanation_subjects
    config = app.state.config
    native = config.database_url.startswith("postgres")
    version = "0148" if native else "0145"
    headers, _ = await _legacy(app, f)
    response = await f.anonymous.get(f"/request-explain/{ids[0]}", headers=headers)
    assert response.status_code == 200, response.json().get("code")
    before = _facts(config)
    rollback_last_migration(config.database_url, expected_version=version)
    assert apply_migrations(config.database_url).applied == [version]
    assert _facts(config) == before
    for forward in (False, True):
        with closing(connect_database(config.database_url)) as connection:
            if native:
                connection.execute(
                    "CREATE FUNCTION test_explanation_drift() RETURNS trigger "
                    "LANGUAGE plpgsql AS 'BEGIN RETURN NEW; END;'"
                )
                connection.execute(
                    "CREATE TRIGGER test_explanation_drift BEFORE INSERT ON "
                    "request_explanations FOR EACH ROW "
                    "EXECUTE FUNCTION test_explanation_drift()"
                )
            else:
                connection.execute(
                    "CREATE TRIGGER test_explanation_drift BEFORE INSERT ON "
                    "request_explanations WHEN 0 BEGIN "
                    "SELECT RAISE(ABORT,'test_explanation_drift'); END"
                )
            connection.commit()
        with pytest.raises(DbError):
            if forward:
                apply_migrations(config.database_url)
            else:
                rollback_last_migration(config.database_url, expected_version=version)
        with closing(connect_database(config.database_url)) as connection:
            connection.execute(
                "DROP TRIGGER test_explanation_drift"
                + (" ON request_explanations" if native else "")
            )
            if native:
                connection.execute("DROP FUNCTION test_explanation_drift()")
            connection.commit()
        if forward:
            assert apply_migrations(config.database_url).applied == [version]
        else:
            rollback_last_migration(config.database_url, expected_version=version)
    assert _facts(config) == before
