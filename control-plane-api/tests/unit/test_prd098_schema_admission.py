"""SaaS schema admission retains all tables and the caller's transaction."""

import sqlite3
from contextlib import closing

import pytest
from metahub_control.saas_billing import service
from metahub_shared.db import connect_database
from metahub_shared.runtime.errors import ApiError

REQUIRED = ("saas_plan_catalogs", "saas_plan_versions", "workspace_subscriptions",
            "subscription_trials", "subscription_order_records", "entitlement_snapshots",
            "saas_subscription_events")


@pytest.fixture
def schema_database(tmp_path):
    with closing(connect_database(f"sqlite:///{(tmp_path / 'schema.db').as_posix()}")) as db:
        for name in REQUIRED:
            db.execute(f'CREATE TABLE "{name}"(id INTEGER)')
        db.commit()
        yield db


def test_schema_check_uses_one_statement_and_retains_transaction(schema_database):
    db = schema_database
    db.execute("BEGIN IMMEDIATE")
    statements = []
    db.set_trace_callback(statements.append)
    service._require_schema(db)
    assert len(statements) == 1
    assert db.in_transaction
    db.rollback()


@pytest.mark.parametrize("name", REQUIRED)
def test_each_missing_table_is_rechecked_after_rollback(schema_database, name):
    db = schema_database
    service._require_schema(db)
    db.execute("BEGIN IMMEDIATE")
    db.execute(f'DROP TABLE "{name}"')
    with pytest.raises(ApiError) as error:
        service._require_schema(db)
    assert error.value.code == "saas_catalog.unavailable"
    assert db.in_transaction
    db.rollback()
    service._require_schema(db)


@pytest.mark.parametrize("substitute", ["VIEW", "TEMP TABLE"])
def test_non_main_table_cannot_replace_required_table(schema_database, substitute):
    db = schema_database
    name = REQUIRED[0]
    db.execute(f'DROP TABLE "{name}"')
    if substitute == "VIEW":
        db.execute(f'CREATE VIEW "{name}" AS SELECT 1 AS id')
    else:
        db.execute(f'CREATE TEMP TABLE "{name}"(id INTEGER)')
    with pytest.raises(ApiError) as error:
        service._require_schema(db)
    assert error.value.code == "saas_catalog.unavailable"


def test_schema_database_failure_is_not_success(schema_database):
    schema_database.close()
    with pytest.raises(sqlite3.Error):
        service._require_schema(schema_database)
