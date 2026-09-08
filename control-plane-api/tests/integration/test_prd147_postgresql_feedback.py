"""Workspace feedback contracts against an isolated native PostgreSQL database."""

import sqlite3
import time
from contextlib import aclosing, closing
from pathlib import Path

import g6_postgresql_support as native_support
import pytest
import test_postgresql_native_migrations as native_migrations
import test_prd147_workspace_feedback as contracts
from metahub_shared.db.connection import connect_database, database_operation_deadline
from metahub_shared.runtime.errors import ApiError
from test_prd147_workspace_files import workspace_http_subjects

pytestmark = native_migrations.pytestmark
native_business_template = native_support.native_business_template
native_business_database = native_support.native_business_database
feedback_subjects = contracts.feedback_subjects


@pytest.fixture
async def file_subjects(native_business_database, tmp_path, monkeypatch):
    async with aclosing(
        workspace_http_subjects(
            native_business_database,
            tmp_path,
            monkeypatch,
        )
    ) as subjects:
        async for app, fixtures in subjects:
            yield app, native_business_database, fixtures


test_workspace_feedback_create_replay_scope_and_summary = (
    contracts.test_workspace_feedback_create_replay_scope_and_summary
)
test_feedback_commit_replay_and_conflict_recheck_expiry = (
    contracts.test_feedback_commit_replay_and_conflict_recheck_expiry
)
test_feedback_summary_checks_new_snapshot_revocation = (
    contracts.test_feedback_summary_checks_new_snapshot_revocation
)
test_feedback_write_does_not_block_event_loop = (
    contracts.test_feedback_write_does_not_block_event_loop
)


@pytest.mark.anyio
async def test_postgresql_workspace_authentication_lock_budget(feedback_subjects):
    app, _, f, _ = feedback_subjects
    with closing(connect_database(app.state.config.database_url)) as locker:
        locker.execute("LOCK workspace_sessions IN ACCESS EXCLUSIVE MODE")
        started = time.monotonic()
        with pytest.raises(ApiError) as error:
            contracts.workspace_access.authenticate_workspace_session_as_auth_context(
                app.state.config,
                f.source.cookies.get(contracts.WORKSPACE_SESSION),
                monotonic_deadline=started + 0.060,
                deadline_error_code="feedback.unavailable",
            )
        assert error.value.code == "feedback.unavailable"
        assert time.monotonic() - started < 0.250
        locker.rollback()
    assert contracts.workspace_access.authenticate_workspace_session_as_auth_context(
        app.state.config,
        f.source.cookies.get(contracts.WORKSPACE_SESSION),
    )


def test_postgresql_deadline_restores_settings_and_cancels_statement(native_business_database):
    with closing(connect_database(native_business_database)) as connection:
        previous = connection.execute("SHOW statement_timeout").fetchone()[0]
        connection.rollback()
        started = time.monotonic()
        with pytest.raises(sqlite3.Error), database_operation_deadline(started + 0.060):
            connection.execute("SELECT pg_sleep(1)")
        connection.rollback()
        assert time.monotonic() - started < 0.250
        assert connection.execute("SHOW statement_timeout").fetchone()[0] == previous


@pytest.mark.anyio
async def test_feedback_owner_migration_preserves_facts_and_rejects_drift(feedback_subjects):
    app, _, f, ids = feedback_subjects
    response = await f.source.post(
        "/feedback", headers=contracts._headers(f.source), json=contracts._payload(ids[0]),
    )
    assert response.status_code == 201, response.json().get("code")
    before = contracts._facts(app.state.config)
    root = Path(__file__).resolve().parents[3] / "infra/migrations/postgresql"
    forward = (root / "0147_feedback_deferred_owner.sql").read_text(encoding="utf-8")
    backward = (root / "0147_feedback_deferred_owner.down.sql").read_text(encoding="utf-8")
    with closing(connect_database(app.state.config.database_url)) as connection:
        manifests = [tuple(row) for row in connection.execute(
            "SELECT * FROM tenant_deletion_table_manifest WHERE source_table IN "
            "('request_feedbacks','request_feedback_events') ORDER BY source_table"
        )]
        connection.rollback()
        connection.execute(backward)
        connection.commit()
        assert contracts._facts(app.state.config) == before
        connection.execute(forward)
        connection.commit()
        assert contracts._facts(app.state.config) == before
        assert manifests == [tuple(row) for row in connection.execute(
            "SELECT * FROM tenant_deletion_table_manifest WHERE source_table IN "
            "('request_feedbacks','request_feedback_events') ORDER BY source_table"
        )]
        connection.rollback()
        connection.execute(backward)
        connection.commit()
        connection.execute(
            "ALTER TABLE request_feedbacks DROP CONSTRAINT "
            "fk_request_feedbacks_0_request_feedback_events"
        )
        with pytest.raises(sqlite3.Error):
            connection.execute(forward)
        connection.rollback()
        connection.execute(forward)
        connection.commit()
    assert contracts._facts(app.state.config) == before
