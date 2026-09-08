"""Native page-visit migration and actual Cookie API smoke, not full G7 QA."""

import hashlib
import json
from contextlib import aclosing, closing
from time import monotonic

import g6_postgresql_support as native_support
import pytest
import test_postgresql_native_migrations as native_migrations
import test_prd147_workspace_page_visits as visits
from metahub_assurance_runner import bootstrap
from metahub_control.workspace_navigation import routes
from metahub_shared.db import (
    DbError,
    apply_migrations,
    connect_database,
    rollback_last_migration,
)
from metahub_shared.db import connection as database_connection
from test_prd147_workspace_files import workspace_http_subjects

pytestmark = native_migrations.pytestmark
native_business_template = native_support.native_business_template
native_business_database = native_support.native_business_database

AUTHORIZATION_TABLES = ("invitations", "invitation_events", "audit_logs", "product_events")


def _row_hashes(rows):
    return sorted(
        hashlib.sha256(json.dumps(dict(row), sort_keys=True, default=str).encode()).hexdigest()
        for row in rows
    )


def _authorization_state(database):
    with closing(connect_database(database)) as connection:
        facts = {
            table: _row_hashes(connection.execute(f"SELECT * FROM {table}").fetchall())
            for table in AUTHORIZATION_TABLES
        }
        placeholders = ",".join("?" for _ in AUTHORIZATION_TABLES)
        for table in ("tenant_deletion_trigger_manifest", "tenant_deletion_table_manifest"):
            facts[table] = _row_hashes(connection.execute(
                f"SELECT * FROM {table} WHERE source_table IN({placeholders})",
                AUTHORIZATION_TABLES,
            ).fetchall())
        facts["live_triggers"] = _row_hashes(connection.execute(
            "SELECT relation.relname,trigger.tgname,trigger.tgenabled,"
            "pg_get_triggerdef(trigger.oid),pg_get_functiondef(trigger.tgfoid) "
            "FROM pg_trigger trigger JOIN pg_class relation ON relation.oid=trigger.tgrelid "
            "WHERE relation.relnamespace=current_schema()::regnamespace "
            f"AND NOT trigger.tgisinternal AND relation.relname IN({placeholders})",
            AUTHORIZATION_TABLES,
        ).fetchall())
    return facts


def test_native_authorization_repair_roundtrip_preserves_facts_and_manifests(
    native_business_database,
):
    database = native_business_database
    before = _authorization_state(database)
    assert rollback_last_migration(database, expected_version="0144").rolled_back == "0144"
    assert rollback_last_migration(database, expected_version="0143").rolled_back == "0143"
    legacy = _authorization_state(database)
    assert {table: legacy[table] for table in AUTHORIZATION_TABLES} == {
        table: before[table] for table in AUTHORIZATION_TABLES
    }
    assert legacy["live_triggers"] != before["live_triggers"]
    assert apply_migrations(database).applied == ["0143", "0144"]
    assert _authorization_state(database) == before
    assert apply_migrations(database).applied == []


@pytest.mark.parametrize("direction", ["up", "down"])
@pytest.mark.parametrize("drift", [
    "function", "function_settings", "disabled", "trigger_manifest", "table_manifest",
])
def test_native_authorization_repair_refuses_drift_atomically(
    native_business_database, direction, drift,
):
    database = native_business_database
    rollback_last_migration(database, expected_version="0144")
    if direction == "up":
        rollback_last_migration(database, expected_version="0143")
    with closing(connect_database(database)) as connection:
        if drift == "function":
            connection.execute(
                "CREATE OR REPLACE FUNCTION fn_trg_invitations_owner_insert_prd060() "
                "RETURNS trigger LANGUAGE plpgsql AS $$BEGIN RETURN NEW; END;$$;"
            )
        elif drift == "function_settings":
            connection.execute(
                "ALTER FUNCTION fn_trg_invitations_owner_insert_prd060() SET search_path=pg_catalog"
            )
        elif drift == "disabled":
            connection.execute(
                "ALTER TABLE invitations DISABLE TRIGGER trg_invitations_owner_insert_prd060"
            )
        else:
            if drift == "trigger_manifest":
                table = "tenant_deletion_trigger_manifest"
                column = "trigger_sha256"
            else:
                table = "tenant_deletion_table_manifest"
                column = "trigger_set_sha256"
            trigger = f"trg_{table}_update_prd089"
            connection.execute(f"ALTER TABLE {table} DISABLE TRIGGER {trigger}")
            connection.execute(
                f"UPDATE {table} SET {column}=repeat('0',64) WHERE source_table='invitations'"
            )
            connection.execute(f"ALTER TABLE {table} ENABLE TRIGGER {trigger}")
        connection.commit()
    before = _authorization_state(database)
    with pytest.raises(DbError) as rejected:
        if direction == "up":
            apply_migrations(database)
        else:
            rollback_last_migration(database, expected_version="0143")
    assert rejected.value.code == "db.migration_failed"
    assert _authorization_state(database) == before


@pytest.fixture
async def native_visit_subjects(native_business_database, tmp_path, monkeypatch, record_property):
    original = bootstrap.guarded_request

    async def diagnostic_request(guard, client, method, path, **kwargs):
        started = monotonic()
        try:
            response = await original(guard, client, method, path, **kwargs)
        except Exception as error:
            raise pytest.fail.Exception(
                f"native bootstrap {method} {path}: {type(error).__name__}; "
                f"{getattr(database_connection, '_g6_last_native_diagnostic', None)}",
                pytrace=False,
            ) from None
        finally:
            record_property(f"bootstrap {method} {path} seconds", round(monotonic() - started, 3))
        if response.is_error:
            pytest.fail(
                f"native bootstrap {method} {path}: {response.status_code} "
                f"{response.json().get('code')}; "
                f"{getattr(database_connection, '_g6_last_native_diagnostic', None)}",
                pytrace=False,
            )
        return response

    monkeypatch.setattr(bootstrap, "guarded_request", diagnostic_request)
    async with aclosing(
        workspace_http_subjects(
            native_business_database,
            tmp_path,
            monkeypatch,
        )
    ) as subjects:
        async for app, fixtures in subjects:
            app.include_router(routes.router)
            yield app, native_business_database, fixtures


@pytest.mark.anyio
async def test_native_page_visit_cookie_and_unique_facts(native_visit_subjects):
    _app, database, _fixtures = native_visit_subjects
    before = _authorization_state(database)
    assert before["invitations"]
    rollback_last_migration(database, expected_version="0144")
    with pytest.raises(DbError):
        rollback_last_migration(database, expected_version="0143")
    assert _authorization_state(database) == before
    assert apply_migrations(database).applied == ["0144"]
    await visits.test_workspace_page_visits_share_original_facts_and_replay(native_visit_subjects)


@pytest.mark.anyio
async def test_native_page_visit_strict_owner_and_transport(native_visit_subjects):
    await visits.test_page_visit_strict_transport_owner_and_retired_routes(native_visit_subjects)


@pytest.mark.anyio
async def test_native_page_visit_history_and_rollback(native_visit_subjects, monkeypatch):
    original = database_connection._raise_postgres_error
    rejected = []
    allowed_codes = {
        "workspace_page_visits_rollback_has_facts",
        "workspace_page_visit_owner_invalid",
        "workspace_page_visit_immutable",
    }

    def capture_rejection(error):
        message = getattr(getattr(error, "diag", None), "message_primary", None)
        rejected.append((
            getattr(error, "sqlstate", None), message if message in allowed_codes else "other",
        ))
        original(error)

    monkeypatch.setattr(database_connection, "_raise_postgres_error", capture_rejection)
    await visits.test_page_visit_migration_preserves_legacy_rows_and_guards_owner(
        native_visit_subjects
    )
    assert rejected == [
        ("23514", "workspace_page_visits_rollback_has_facts"),
        ("23514", "workspace_page_visit_owner_invalid"),
        ("23514", "workspace_page_visit_immutable"),
    ]
