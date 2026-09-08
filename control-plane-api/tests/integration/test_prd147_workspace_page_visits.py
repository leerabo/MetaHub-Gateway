"""Actual Workspace page visits share historical facts and reject stale owners."""

import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from metahub_control.workspace_access import service as workspace_access
from metahub_control.workspace_navigation import routes, service
from metahub_shared.db import apply_migrations, rollback_last_migration
from metahub_shared.db.connection import connect_database
from metahub_shared.db.errors import DbError
from metahub_shared.db.schema import foreign_key_violation_count
from metahub_shared.runtime.errors import ApiError
from test_prd147_workspace_files import (  # noqa: F401
    _headers,
    file_subjects,
    migrated_baseline,
)

ENDPOINT = "/workspace/navigation/page-views"


@pytest.fixture
async def visit_subjects(file_subjects):  # noqa: F811
    app, database, fixtures = file_subjects
    app.include_router(routes.router)
    return app, database, fixtures


async def _payload(client, route="workspace.gateway.provider-health"):
    response = await client.get("/workspace/navigation")
    assert response.status_code == 200, response.json().get("code")
    navigation = response.json()
    return {
        **{
            key: navigation[key]
            for key in (
                "page_visit_session_ref",
                "contract_snapshot_id",
                "permission_fingerprint",
                "navigation_epoch",
            )
        },
        "route_code": route,
        "source": "direct_hash",
        "viewport_class": "desktop",
    }


def _visit_headers(client, key=None, **kwargs):
    return {**_headers(client, **kwargs), "Idempotency-Key": key or uuid4().hex}


def _events(database):
    url = f"sqlite:///{database.as_posix()}" if isinstance(database, Path) else database
    with closing(connect_database(url)) as connection:
        projection = (
            "*"
            if getattr(connection, "backend", "sqlite") == "postgresql"
            else "rowid AS _sqlite_rowid,*"
        )
        return {
            row["event_id"]: {column: row[column] for column in row.keys()}
            for row in connection.execute(
                f"SELECT {projection} FROM console_page_events ORDER BY event_id",
            ).fetchall()
        }


@pytest.mark.anyio
async def test_workspace_page_visits_share_original_facts_and_replay(visit_subjects):
    app, database, fixtures = visit_subjects
    body = await _payload(fixtures.source)
    before = _events(database)
    for route, event_type, source in (
        ("workspace.gateway.provider-health", "status.viewed", "provider-health"),
        ("workspace.home.overview", "dashboard.viewed", "overview"),
        ("workspace.gateway.projects", "console.page_viewed", "projects"),
        ("workspace.developer.migration", "migration.guide_viewed", "migration-guide"),
    ):
        body["route_code"] = route
        key = uuid4().hex
        headers = _visit_headers(fixtures.source, key)
        first = await fixtures.source.post(ENDPOINT, json=body, headers=headers)
        assert first.status_code == 200, first.json().get("code")
        assert first.json()["accepted"] and not first.json()["idempotent_replay"]
        assert "no-store" in first.headers["cache-control"]
        second = await fixtures.source.post(ENDPOINT, json=body, headers=headers)
        assert second.status_code == 200 and second.json()["idempotent_replay"]
        assert second.json()["event_id"] == first.json()["event_id"]
        conflict = await fixtures.source.post(
            ENDPOINT,
            json={**body, "viewport_class": "mobile"},
            headers=headers,
        )
        assert conflict.status_code == 409
        with closing(connect_database(app.state.config.database_url)) as connection:
            row = connection.execute(
                "SELECT * FROM console_page_events WHERE event_id=?",
                (first.json()["event_id"],),
            ).fetchone()
            assert row["event_type"] == event_type and row["navigation_item_id"] == source
            assert row["session_id"] is None and row["workspace_session_id"].startswith("wss_")
            assert row["path"] == f"/#{source}" and row["target_section"] == source
            assert json.loads(row["payload_json"]) == {
                "hash": f"#{source}",
                "source": "direct_hash",
                "viewport_class": "desktop",
            }
            assert foreign_key_violation_count(connection) == 0
    after = _events(database)
    assert {event_id: after[event_id] for event_id in before} == before
    assert len(after) == len(before) + 4


@pytest.mark.anyio
async def test_page_visit_strict_transport_owner_and_retired_routes(visit_subjects):
    _app, database, fixtures = visit_subjects
    body = await _payload(fixtures.source)
    before = _events(database)
    valid_headers = _visit_headers(fixtures.source, attack=True)
    for headers, status in (
        ({}, 403),
        ({**valid_headers, "Authorization": "Bearer invalid"}, 400),
        ({**valid_headers, "Origin": "https://foreign.test"}, 403),
        ({**valid_headers, "X-CSRF-Token": "invalid"}, 403),
        (_visit_headers(fixtures.target, attack=True), 403),
    ):
        response = await fixtures.anonymous.post(ENDPOINT, json=body, headers=headers)
        assert response.status_code == status, response.json().get("code")
    for change, status in (
        ({"route_code": "platform.operations.growth"}, 403),
        ({"route_code": "workspace.help.competitor"}, 403),
        ({"path": "/#provider-health?secret=private"}, 422),
        ({"payload": {"token": "must-not-persist"}}, 422),
        ({"source": "arbitrary"}, 422),
        ({"viewport_class": 1}, 422),
        ({"navigation_epoch": True}, 422),
        ({"navigation_epoch": body["navigation_epoch"] + 1}, 412),
        ({"page_visit_session_ref": "f" * 64}, 403),
        ({"contract_snapshot_id": "ncs_" + "f" * 32}, 412),
    ):
        response = await fixtures.source.post(
            ENDPOINT,
            json={**body, **change},
            headers=_visit_headers(fixtures.source),
        )
        assert response.status_code == status, response.json().get("code")
    duplicate = json.dumps(body)[:-1] + ',"source":"sidebar"}'
    response = await fixtures.source.post(
        ENDPOINT,
        content=duplicate,
        headers={**_visit_headers(fixtures.source), "Content-Type": "application/json"},
    )
    assert response.status_code == 422
    response = await fixtures.source.post(
        ENDPOINT + "?tenant_id=foreign",
        json=body,
        headers=_visit_headers(fixtures.source),
    )
    assert response.status_code == 422 and _events(database) == before


@pytest.mark.anyio
async def test_page_visit_replay_reauthenticates_and_rejects_changed_session(visit_subjects):
    _app, database, fixtures = visit_subjects
    body = await _payload(fixtures.source)
    key = uuid4().hex
    first = await fixtures.source.post(
        ENDPOINT, json=body, headers=_visit_headers(fixtures.source, key)
    )
    assert first.status_code == 200
    before = _events(database)
    switched = await fixtures.source.post(
        "/identity/workspace-switches",
        json={"workspace_id": fixtures.source_workspace_id},
        headers=_headers(fixtures.source, global_session=True),
    )
    assert switched.status_code == 200
    stale = await fixtures.source.post(
        ENDPOINT, json=body, headers=_visit_headers(fixtures.source, key)
    )
    assert stale.status_code == 403
    fresh_body = await _payload(fixtures.source)
    assert fresh_body["page_visit_session_ref"] != body["page_visit_session_ref"]
    revoked_headers = _visit_headers(fixtures.source, key, attack=True)
    logout = await fixtures.source.post(
        "/identity/logout",
        headers=_headers(fixtures.source, global_session=True),
    )
    assert logout.status_code == 200
    denied = await fixtures.source.post(
        ENDPOINT,
        json=fresh_body,
        headers=revoked_headers,
    )
    assert denied.status_code == 401 and _events(database) == before


@pytest.mark.anyio
async def test_page_visit_final_rejection_rolls_back_fact(visit_subjects, monkeypatch):
    _app, database, fixtures = visit_subjects
    body = await _payload(fixtures.source)
    original = service._page_visit_authority
    calls = []

    def authority(*args, **kwargs):
        actor = original(*args, **kwargs)
        calls.append(True)
        if len(calls) == 2:
            raise ApiError("workspace_session.revoked")
        return actor

    monkeypatch.setattr(service, "_page_visit_authority", authority)
    before = _events(database)
    denied = await fixtures.source.post(
        ENDPOINT, json=body, headers=_visit_headers(fixtures.source)
    )
    assert denied.status_code == 401 and calls == [True, True]
    assert _events(database) == before


@pytest.mark.anyio
async def test_page_visit_actual_downgrade_rejects_old_and_new_navigation(visit_subjects):
    _app, database, fixtures = visit_subjects
    body = await _payload(fixtures.member)
    headers = _visit_headers(fixtures.member, attack=True)
    changed = await fixtures.source.patch(
        f"/memberships/{fixtures.member_membership_id}",
        json={"role": "viewer"},
        headers={**_headers(fixtures.source), "If-Match": f'"{fixtures.member_revision}"'},
    )
    assert changed.status_code == 200
    before = _events(database)
    denied = await fixtures.anonymous.post(ENDPOINT, json=body, headers=headers)
    assert denied.status_code in (401, 403)
    switched = await fixtures.member.post(
        "/identity/workspace-switches",
        json={"workspace_id": fixtures.source_workspace_id},
        headers=_headers(fixtures.member, global_session=True),
    )
    assert switched.status_code == 200
    body = await _payload(fixtures.member)
    denied = await fixtures.member.post(
        ENDPOINT, json=body, headers=_visit_headers(fixtures.member)
    )
    assert denied.status_code == 403 and _events(database) == before


@pytest.mark.anyio
async def test_page_visit_real_expiry_at_final_authentication_rolls_back(
    visit_subjects, monkeypatch
):
    _app, database, fixtures = visit_subjects
    body = await _payload(fixtures.source)
    original = service._page_visit_authority
    now = workspace_access._utcnow()
    calls = []

    def authority(*args, **kwargs):
        if calls:
            monkeypatch.setattr(workspace_access, "_utcnow", lambda: now + timedelta(days=2))
        calls.append(True)
        return original(*args, **kwargs)

    monkeypatch.setattr(service, "_page_visit_authority", authority)
    before = _events(database)
    denied = await fixtures.source.post(
        ENDPOINT, json=body, headers=_visit_headers(fixtures.source)
    )
    assert denied.status_code == 401 and calls == [True, True]
    assert _events(database) == before


@pytest.mark.anyio
async def test_page_visit_migration_preserves_legacy_rows_and_guards_owner(visit_subjects):
    app, database, fixtures = visit_subjects
    body = await _payload(fixtures.source)
    url = app.state.config.database_url
    with closing(connect_database(url)) as connection:
        actor = connection.execute(
            "SELECT tenant_id,legacy_user_id FROM workspace_memberships WHERE workspace_id=?",
            (fixtures.source_workspace_id,),
        ).fetchone()
        connection.execute(
            """INSERT INTO console_page_events(event_id,tenant_id,user_id,event_type,
                 navigation_item_id,target_section,path,request_id,payload_json,created_at)
               VALUES('cpe_historical',?,?,'console.page_viewed','overview','overview',
                 '/#overview','old-request','{ "source": "sidebar" }','2026-01-01T00:00:00Z')""",
            (actor["tenant_id"], actor["legacy_user_id"]),
        )
        connection.commit()
    before = _events(database)
    rollback_last_migration(url)
    apply_migrations(url)
    assert _events(database) == before
    with closing(connect_database(url)) as connection:
        manifest = connection.execute(
            "SELECT * FROM tenant_deletion_table_manifest WHERE source_table='console_page_events'",
        ).fetchone()
        triggers = connection.execute(
            "SELECT * FROM tenant_deletion_trigger_manifest "
            "WHERE source_table='console_page_events' "
            "ORDER BY trigger_name",
        ).fetchall()
        assert len(triggers) == 2
        for trigger in triggers:
            assert (
                hashlib.sha256(trigger["trigger_sql"].encode()).hexdigest()
                == trigger["trigger_sha256"]
            )
        combined = "\x1e".join(
            f"{row['trigger_name']}\x1f{row['trigger_sha256']}" for row in triggers
        )
        assert hashlib.sha256(combined.encode()).hexdigest() == manifest["trigger_set_sha256"]
    accepted = await fixtures.source.post(
        ENDPOINT, json=body, headers=_visit_headers(fixtures.source)
    )
    assert accepted.status_code == 200, accepted.json().get("code")
    with pytest.raises(DbError):
        rollback_last_migration(url)
    assert len(_events(database)) == len(before) + 1
    with closing(connect_database(url)) as connection:
        row = connection.execute(
            "SELECT * FROM console_page_events WHERE event_id=?",
            (accepted.json()["event_id"],),
        ).fetchone()
        foreign = connection.execute(
            "SELECT tenant_id,legacy_user_id FROM workspace_memberships WHERE workspace_id=?",
            (fixtures.target_workspace_id,),
        ).fetchone()
        native = getattr(connection, "backend", "sqlite") == "postgresql"
        with pytest.raises(sqlite3.IntegrityError, match=None if native else "owner_invalid"):
            connection.execute(
                """INSERT INTO console_page_events(event_id,tenant_id,user_id,event_type,
                   navigation_item_id,target_section,path,request_id,created_at,
                   workspace_session_id,visit_request_sha256)
                   VALUES('cpe_foreign',?,?,'status.viewed','provider-health','provider-health',
                     '/#provider-health','bad-request','2026-01-01T00:00:00Z',?,?)""",
                (
                    foreign["tenant_id"],
                    foreign["legacy_user_id"],
                    row["workspace_session_id"],
                    "a" * 64,
                ),
            )
        connection.rollback()
        with pytest.raises(sqlite3.IntegrityError, match=None if native else "immutable"):
            connection.execute(
                "UPDATE console_page_events SET path='/changed' WHERE event_id=?",
                (row["event_id"],),
            )
        connection.rollback()
        assert foreign_key_violation_count(connection) == 0
