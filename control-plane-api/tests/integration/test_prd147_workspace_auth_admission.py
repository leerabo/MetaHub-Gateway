"""Read-first Workspace authentication keeps write effects on a fresh snapshot."""

import time
from contextlib import closing
from datetime import timedelta

import pytest
import test_prd147_workspace_files as file_contracts
from metahub_control.workspace_access import service as workspace_access
from metahub_shared.db.connection import connect_database, database_request_scope
from metahub_shared.runtime.errors import ApiError
from test_prd147_workspace_files import WORKSPACE_SESSION

file_subjects = file_contracts.file_subjects
migrated_baseline = file_contracts.migrated_baseline


def _session(config, token):
    return workspace_access.authenticate_workspace_session_context(config, token)


def _age_session(config, session_id):
    with closing(connect_database(config.database_url)) as connection:
        connection.execute(
            "UPDATE workspace_sessions SET last_seen_at=? WHERE workspace_session_id=?",
            (workspace_access._db_time(workspace_access._utcnow() - timedelta(seconds=61)),
             session_id),
        )
        connection.commit()


@pytest.mark.anyio
async def test_recent_workspace_authentication_does_not_wait_for_sqlite_writer(file_subjects):
    app, _, fixtures = file_subjects
    config = app.state.config
    token = fixtures.source.cookies.get(WORKSPACE_SESSION)
    with closing(connect_database(config.database_url)) as writer:
        with database_request_scope(reuse_connections=True):
            expected = _session(config, token)
            writer.execute("BEGIN IMMEDIATE")
            try:
                started = time.monotonic()
                actual = workspace_access.authenticate_workspace_session_context(
                    config, token, monotonic_deadline=started + 0.250,
                    deadline_error_code="feedback.unavailable",
                )
                assert actual.workspace_session_id == expected.workspace_session_id
                assert time.monotonic() - started < 0.250
            finally:
                writer.rollback()


@pytest.mark.anyio
async def test_due_workspace_touch_uses_read_then_fresh_write(file_subjects, monkeypatch):
    app, _, fixtures = file_subjects
    config = app.state.config
    token = fixtures.source.cookies.get(WORKSPACE_SESSION)
    context = _session(config, token)
    _age_session(config, context.workspace_session_id)
    original = workspace_access._authenticate_workspace
    phases = []

    def capture(connection, *args, **kwargs):
        phases.append((kwargs.get("read_only", False), kwargs["touch"]))
        return original(connection, *args, **kwargs)

    monkeypatch.setattr(workspace_access, "_authenticate_workspace", capture)
    assert _session(config, token).workspace_session_id == context.workspace_session_id
    assert phases == [(True, False), (False, True)]
    phases.clear()
    assert _session(config, token).workspace_session_id == context.workspace_session_id
    assert phases == [(True, False)]


@pytest.mark.anyio
async def test_workspace_touch_rechecks_revocation_after_read(file_subjects, monkeypatch):
    app, _, fixtures = file_subjects
    config = app.state.config
    token = fixtures.source.cookies.get(WORKSPACE_SESSION)
    context = _session(config, token)
    _age_session(config, context.workspace_session_id)
    original = workspace_access._authenticate_workspace
    phases = []

    with closing(connect_database(config.database_url)) as revoker:
        if not config.database_url.startswith("postgres"):
            assert revoker.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        revoker.execute("SELECT 1 FROM workspace_sessions LIMIT 1").fetchone()
        revoker.rollback()

        def revoke_after_read(connection, *args, **kwargs):
            phases.append(kwargs.get("read_only", False))
            result = original(connection, *args, **kwargs)
            if kwargs.get("read_only"):
                revoker.execute(
                    "UPDATE workspace_sessions SET status='revoked',revoked_reason='logout',"
                    "revoked_at=? WHERE workspace_session_id=?",
                    (workspace_access._db_time(workspace_access._utcnow()),
                     context.workspace_session_id),
                )
                revoker.commit()
            return result

        monkeypatch.setattr(workspace_access, "_authenticate_workspace", revoke_after_read)
        with pytest.raises(ApiError) as error:
            _session(config, token)
    assert error.value.code == "workspace_session.revoked"
    assert phases == [True, False]


@pytest.mark.anyio
async def test_workspace_read_denial_is_rechecked_before_persisting(file_subjects, monkeypatch):
    app, _, fixtures = file_subjects
    config = app.state.config
    token = fixtures.source.cookies.get(WORKSPACE_SESSION)
    context = _session(config, token)
    original = workspace_access._permission_snapshot
    attempts = 0

    def deny_old_snapshot(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ApiError("auth.permission_denied")
        return original(*args, **kwargs)

    monkeypatch.setattr(workspace_access, "_permission_snapshot", deny_old_snapshot)
    assert _session(config, token).workspace_session_id == context.workspace_session_id
    assert attempts == 2
    with closing(connect_database(config.database_url)) as connection:
        row = connection.execute(
            "SELECT status,revoked_reason FROM workspace_sessions WHERE workspace_session_id=?",
            (context.workspace_session_id,),
        ).fetchone()
    assert tuple(row) == ("active", None)
