"""Real Workspace log reads, personal filters and audited exports."""

import hashlib
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from metahub_assurance_runner.probes import WORKSPACE_SESSION
from metahub_control.audit import routes as audit_routes
from metahub_control.audit.export_outbox import (
    claim_next_audit_export_event,
    dispatch_claimed_audit_export_event,
)
from metahub_control.auth.resource_transactions import ResourceWriteTransaction
from metahub_control.auth.routes import router as legacy_auth_router
from metahub_control.request_logs import outbox
from metahub_control.request_logs import routes as log_routes
from metahub_control.workspace_access import service as workspace_access
from metahub_shared.db import apply_migrations, rollback_last_migration
from metahub_shared.db.connection import connect_database
from metahub_shared.db.errors import DbError
from metahub_shared.db.schema import foreign_key_violation_count
from test_prd147_workspace_files import (  # noqa: F401
    _headers,
    file_subjects,
    migrated_baseline,
)


@pytest.fixture
async def log_subjects(file_subjects):  # noqa: F811
    app, database, fixtures = file_subjects
    app.include_router(log_routes.router)
    app.include_router(audit_routes.router)
    return app, database, fixtures


def _facts(config):
    with closing(connect_database(config.database_url)) as connection:
        return {
            table: [tuple(row) for row in connection.execute(f"SELECT * FROM {table}")]
            for table in (
                "request_log_query_events",
                "saved_filters",
                "audit_exports",
                "audit_logs",
                "product_events",
            )
        }


async def _legacy(app, f):
    app.include_router(legacy_auth_router)
    workspace = workspace_access.authenticate_workspace_session_as_auth_context(
        app.state.config,
        f.source.cookies.get(WORKSPACE_SESSION),
    )
    response = await f.anonymous.post(
        "/auth/login",
        json={
            "tenant_slug": workspace.tenant.slug,
            "email": f.source_account.email,
            "password": f.source_account.password,
        },
    )
    assert response.status_code == 200
    return {"Authorization": "Bearer " + response.json()["access_token"]}, workspace


@pytest.mark.anyio
async def test_workspace_query_fact_and_personal_filter_ownership(log_subjects):
    app, _database, f = log_subjects
    response = await f.source.get("/request-logs")
    assert response.status_code == 200, response.json().get("code")
    event_id = response.json()["query_event_id"]
    with closing(connect_database(app.state.config.database_url)) as connection:
        row = connection.execute(
            "SELECT * FROM request_log_query_events WHERE query_event_id=?",
            (event_id,),
        ).fetchone()
        assert row["actor_session_id"] is None
        assert row["workspace_session_id"].startswith("wss_")
        assert (
            connection.execute(
                "SELECT count(*) FROM product_events WHERE source_id=?",
                (event_id,),
            ).fetchone()[0]
            == 1
        )
    created = await f.source.post(
        "/request-logs/saved-filters",
        headers=_headers(f.source),
        json={
            "name": "Owned logs",
            "filters": {"v": 1, "status": "success"},
            "sort_by": "created_at",
            "sort_direction": "desc",
        },
    )
    assert created.status_code == 201, created.json().get("code")
    saved = created.json()["saved_filter"]
    identifier = saved["saved_filter_id"]
    own = await f.source.get("/request-logs", params={"saved_filter_id": identifier})
    assert own.status_code == 200
    for client in (f.target, f.member):
        denied = await client.get("/request-logs", params={"saved_filter_id": identifier})
        assert denied.status_code == 404
    updated = await f.source.patch(
        f"/request-logs/saved-filters/{identifier}",
        headers=_headers(f.source),
        json={"version": saved["version"], "name": "Updated logs"},
    )
    assert updated.status_code == 200
    removed = await f.source.delete(
        f"/request-logs/saved-filters/{identifier}",
        headers=_headers(f.source),
        params={"version": updated.json()["saved_filter"]["version"]},
    )
    assert removed.status_code == 204
    listing = await f.source.get("/request-logs/saved-filters")
    assert listing.status_code == 200 and listing.json()["saved_filters"] == []


@pytest.mark.anyio
async def test_workspace_audit_export_is_csrf_protected_and_replayable(log_subjects):
    app, _database, f = log_subjects
    listing = await f.source.get("/audit-logs")
    assert listing.status_code == 200, listing.json().get("code")
    headers = _headers(f.source)
    for invalid in (
        {},
        {**headers, "X-CSRF-Token": "invalid"},
        {**headers, "Origin": "https://foreign.test"},
    ):
        response = await f.source.get("/audit-logs/export", headers=invalid)
        assert response.status_code == 403, response.json().get("code")
    with closing(connect_database(app.state.config.database_url)) as connection:
        assert connection.execute("SELECT count(*) FROM audit_exports").fetchone()[0] == 0
    headers["X-Request-ID"] = "test_workspace_log_export_147"
    exported = await f.source.get("/audit-logs/export", headers=headers)
    assert exported.status_code == 200, exported.headers.get("content-type")
    assert "text/csv" in exported.headers["content-type"]
    replay = await f.source.get("/audit-logs/export", headers=headers)
    assert replay.status_code == 200 and replay.content == exported.content
    assert (
        replay.headers["X-MetaHub-Audit-Export-ID"] == exported.headers["X-MetaHub-Audit-Export-ID"]
    )
    with closing(connect_database(app.state.config.database_url)) as connection:
        assert connection.execute("SELECT count(*) FROM audit_exports").fetchone()[0] == 1
    claim = claim_next_audit_export_event(app.state.config, worker_id="test-audit-worker")
    assert claim is not None
    assert dispatch_claimed_audit_export_event(app.state.config, claim)


@pytest.mark.anyio
async def test_log_identity_history_roundtrip_and_explicit_legacy(log_subjects):
    app, _database, f = log_subjects
    config = app.state.config
    headers, _workspace = await _legacy(app, f)
    response = await f.anonymous.get("/request-logs", headers=headers)
    assert response.status_code == 200, response.json().get("code")
    version = "0146" if config.database_url.startswith("postgres") else "0144"
    before = _facts(config)
    assert before["request_log_query_events"][0][4] is not None
    rollback_last_migration(config.database_url, expected_version=version)
    assert apply_migrations(config.database_url).applied == [version]
    assert _facts(config) == before
    assert not apply_migrations(config.database_url).applied
    exported = await f.anonymous.get("/audit-logs/export", headers=headers)
    assert exported.status_code == 200
    logout = await f.anonymous.post("/auth/logout", headers=headers)
    assert logout.status_code == 200
    before = _facts(config)
    for path in (
        "/request-logs",
        "/request-logs/saved-filters",
        "/audit-logs",
        "/audit-logs/export",
    ):
        denied = await f.anonymous.get(path, headers=headers)
        assert denied.status_code == 401
    assert _facts(config) == before


@pytest.mark.anyio
async def test_workspace_query_owner_outbox_and_lossless_rollback_guard(log_subjects):
    app, _database, f = log_subjects
    config = app.state.config
    response = await f.source.get("/request-logs")
    assert response.status_code == 200
    event_id = response.json()["query_event_id"]
    target = workspace_access.authenticate_workspace_session_as_auth_context(
        config,
        f.target.cookies.get(WORKSPACE_SESSION),
    )
    with closing(connect_database(config.database_url)) as connection:
        row = dict(
            connection.execute(
                "SELECT * FROM request_log_query_events WHERE query_event_id=?",
                (event_id,),
            ).fetchone()
        )
        for changes in (
            {"workspace_session_id": target.session.session_id},
            {"workspace_session_id": None},
            {"actor_user_id": target.user.user_id},
        ):
            values = {
                **row,
                **changes,
                "query_event_id": "lqe_" + uuid4().hex,
                "event_id": "lqevt_" + uuid4().hex,
            }
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(
                    "INSERT INTO request_log_query_events("
                    + ",".join(values)
                    + ") VALUES("
                    + ",".join("?" for _ in values)
                    + ")",
                    tuple(values.values()),
                )
            connection.rollback()
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE request_log_query_events SET workspace_session_id=? WHERE query_event_id=?",
                (target.session.session_id, event_id),
            )
        connection.rollback()
        assert foreign_key_violation_count(connection) == 0
    claim = outbox.claim_next_request_log_query_event(config, worker_id="test-log-worker")
    assert claim is not None and claim.query_event_id == event_id
    assert outbox.dispatch_claimed_request_log_query_event(config, claim)
    before = _facts(config)
    version = "0146" if config.database_url.startswith("postgres") else "0144"
    with pytest.raises(DbError):
        rollback_last_migration(config.database_url, expected_version=version)
    assert _facts(config) == before


@pytest.mark.anyio
async def test_log_terminal_expiry_rolls_back_all_facts(log_subjects, monkeypatch):
    app, _database, f = log_subjects
    config = app.state.config
    headers, workspace = await _legacy(app, f)
    created = await f.source.post(
        "/request-logs/saved-filters",
        headers=_headers(f.source),
        json={
            "name": "Expiry",
            "filters": {"v": 1},
            "sort_by": "created_at",
            "sort_direction": "desc",
        },
    )
    assert created.status_code == 201
    saved = created.json()["saved_filter"]
    log_id = "req_log_identity_" + uuid4().hex
    with closing(connect_database(config.database_url)) as connection:
        timestamp = datetime.now(UTC).isoformat()
        connection.execute(
            "INSERT INTO gateway_request_claims(tenant_id,request_id,trace_id,virtual_key_id,"
            "snapshot_version,policy_snapshot_status,claimed_at) VALUES(?,?,?,NULL,NULL,'disabled',?)",
            (workspace.user.tenant_id, log_id, log_id, timestamp),
        )
        connection.execute(
            "INSERT INTO request_logs(request_id,tenant_id,trace_id,status,created_at,updated_at) "
            "VALUES(?,?,?,'success',?,?)",
            (log_id, workspace.user.tenant_id, log_id, timestamp, timestamp),
        )
        connection.commit()
    detail = await f.source.get(f"/request-logs/{log_id}")
    assert detail.status_code == 200
    foreign = await f.target.get(f"/request-logs/{log_id}")
    assert foreign.status_code == 404
    export_headers = {**_headers(f.source), "X-Request-ID": "test-expiry-replay-147"}
    assert (await f.source.get("/audit-logs/export", headers=export_headers)).status_code == 200
    original = ResourceWriteTransaction.verify
    future = workspace_access._utcnow() + timedelta(days=2)
    seen = set()

    def expire_on_second_verification(transaction):
        if transaction.connection in seen:
            with monkeypatch.context() as patch:
                patch.setattr(workspace_access, "_utcnow", lambda: future)
                return original(transaction)
        seen.add(transaction.connection)
        return original(transaction)

    before = _facts(config)
    with monkeypatch.context() as patch:
        patch.setattr(ResourceWriteTransaction, "verify", expire_on_second_verification)
        for method, path, kwargs in (
            ("GET", "/request-logs", {}),
            ("GET", f"/request-logs/{log_id}", {}),
            ("GET", "/request-logs/saved-filters", {}),
            ("GET", "/audit-logs", {}),
            (
                "POST",
                "/request-logs/saved-filters",
                {
                    "json": {
                        "name": "Never persisted",
                        "filters": {"v": 1},
                        "sort_by": "created_at",
                        "sort_direction": "desc",
                    }
                },
            ),
            (
                "PATCH",
                f"/request-logs/saved-filters/{saved['saved_filter_id']}",
                {"json": {"name": "Never updated", "version": saved["version"]}},
            ),
            (
                "DELETE",
                f"/request-logs/saved-filters/{saved['saved_filter_id']}",
                {"params": {"version": saved["version"]}},
            ),
            ("GET", "/audit-logs/export", {"headers": export_headers}),
            ("GET", "/audit-logs/export", {}),
        ):
            seen.clear()
            request_headers = {**_headers(f.source, attack=True), **kwargs.pop("headers", {})}
            denied = await f.anonymous.request(method, path, headers=request_headers, **kwargs)
            assert denied.status_code == 401, (method, path, denied.json().get("code"))
            assert denied.json()["code"] == "workspace_session.expired"
            assert _facts(config) == before


@pytest.mark.anyio
async def test_log_migration_rejects_schema_drift_and_preserves_privacy_manifest(log_subjects):
    app, _database, _fixtures = log_subjects
    config = app.state.config
    native = config.database_url.startswith("postgres")
    version = "0146" if native else "0144"
    before = _facts(config)
    for forward in (False, True):
        with closing(connect_database(config.database_url)) as connection:
            if native:
                connection.execute(
                    "CREATE FUNCTION test_log_drift() RETURNS trigger LANGUAGE plpgsql "
                    "AS 'BEGIN RETURN NEW; END;'"
                )
                connection.execute(
                    "CREATE TRIGGER test_log_drift BEFORE INSERT ON request_log_query_events "
                    "FOR EACH ROW EXECUTE FUNCTION test_log_drift()"
                )
            else:
                connection.execute(
                    "CREATE TRIGGER test_log_drift BEFORE INSERT ON request_log_query_events "
                    "WHEN 0 BEGIN SELECT RAISE(ABORT,'test_log_drift'); END"
                )
            connection.commit()
        with pytest.raises(DbError):
            if forward:
                apply_migrations(config.database_url)
            else:
                rollback_last_migration(config.database_url, expected_version=version)
        assert _facts(config) == before
        with closing(connect_database(config.database_url)) as connection:
            connection.execute(
                "DROP TRIGGER test_log_drift" + (" ON request_log_query_events" if native else "")
            )
            if native:
                connection.execute("DROP FUNCTION test_log_drift()")
            connection.commit()
        if forward:
            assert apply_migrations(config.database_url).applied == [version]
        else:
            rollback_last_migration(config.database_url, expected_version=version)
    assert _facts(config) == before
    with closing(connect_database(config.database_url)) as connection:
        for table in (
            "request_log_query_events",
            "audit_exports",
            "audit_export_events",
            "saved_filters",
        ):
            recorded = connection.execute(
                "SELECT trigger_name,trigger_sql,trigger_sha256 "
                "FROM tenant_deletion_trigger_manifest WHERE source_table=? ORDER BY trigger_name",
                (table,),
            ).fetchall()
            assert recorded
            if native:
                actual = dict(
                    connection.execute(
                        "SELECT tgname,pg_get_triggerdef(oid)||';'||chr(10)||pg_get_functiondef(tgfoid) "
                        "FROM pg_trigger WHERE tgrelid=CAST(? AS regclass) AND NOT tgisinternal",
                        (table,),
                    ).fetchall()
                )
            else:
                actual = dict(
                    connection.execute(
                        "SELECT name,sql FROM sqlite_schema WHERE tbl_name=? AND type='trigger'",
                        (table,),
                    ).fetchall()
                )
            assert {row["trigger_name"] for row in recorded} == set(actual)
            for row in recorded:
                assert row["trigger_sql"] == actual[row["trigger_name"]]
                assert (
                    row["trigger_sha256"] == hashlib.sha256(row["trigger_sql"].encode()).hexdigest()
                )
            combined = "\x1e".join(
                f"{row['trigger_name']}\x1f{row['trigger_sha256']}" for row in recorded
            )
            manifest_hash = connection.execute(
                "SELECT trigger_set_sha256 FROM tenant_deletion_table_manifest WHERE source_table=?",
                (table,),
            ).fetchone()[0]
            assert manifest_hash == hashlib.sha256(combined.encode()).hexdigest()
