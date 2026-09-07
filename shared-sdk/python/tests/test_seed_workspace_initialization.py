"""New tenant initialization must not reactivate existing operational states."""

import sqlite3
from contextlib import closing

import pytest
from metahub_shared.db.seed import _ensure_workspace_operational_states, _upsert_tenant


@pytest.fixture
def connection():
    with closing(sqlite3.connect(":memory:")) as database:
        database.row_factory = sqlite3.Row
        database.executescript("""
            CREATE TABLE tenants(tenant_id TEXT PRIMARY KEY,status TEXT);
            CREATE TABLE workspaces(workspace_id TEXT PRIMARY KEY,status TEXT);
            CREATE TABLE workspace_tenant_bindings(workspace_id TEXT,tenant_id TEXT,status TEXT);
            CREATE TABLE tenant_data_states(tenant_id TEXT,lifecycle_status TEXT);
            CREATE TABLE workspace_operational_states(
                workspace_operational_state_id TEXT PRIMARY KEY,workspace_id TEXT UNIQUE,
                tenant_id TEXT,status TEXT,revision INTEGER,risk_revision INTEGER,source TEXT,
                suspended_reason_code TEXT,suspended_by_operator_id TEXT,suspended_at TEXT,
                created_at TEXT,updated_at TEXT);
            INSERT INTO tenants VALUES('tenant-one','active');
            INSERT INTO workspaces VALUES('workspace-one','active');
            INSERT INTO workspace_tenant_bindings VALUES('workspace-one','tenant-one','active');
            INSERT INTO tenant_data_states VALUES('tenant-one','active');
            INSERT INTO workspace_operational_states VALUES(
                'state-one','workspace-one','tenant-one','migration_conflict',1,1,
                'workspace_creation',NULL,NULL,NULL,'2026-09-07T00:00:00Z','2026-09-07T00:00:00Z');
        """)
        yield database


def test_new_tenant_can_finish_its_initial_operational_state(connection):
    _ensure_workspace_operational_states(connection, "tenant-one", initialize_new_tenant=True)
    row = connection.execute("SELECT * FROM workspace_operational_states").fetchone()
    assert (row["status"], row["revision"], row["risk_revision"]) == ("active", 2, 1)
    assert row["workspace_operational_state_id"] == "state-one"
    assert row["created_at"] == "2026-09-07T00:00:00Z"
    before = connection.total_changes
    _ensure_workspace_operational_states(connection, "tenant-one", initialize_new_tenant=True)
    assert connection.total_changes == before


@pytest.mark.parametrize("state", [
    "migration_conflict", "suspended", "deletion_pending", "deleted", "active",
])
def test_existing_tenant_states_are_not_reactivated(connection, state):
    connection.execute("UPDATE workspace_operational_states SET status=?", (state,))
    before = connection.total_changes
    _ensure_workspace_operational_states(connection, "tenant-one")
    assert connection.total_changes == before


@pytest.mark.parametrize("change", [
    "UPDATE workspace_operational_states SET revision=2",
    "UPDATE workspace_operational_states SET risk_revision=2",
    "UPDATE workspace_operational_states SET source='data_lifecycle'",
    "UPDATE workspace_operational_states SET suspended_reason_code='policy'",
    "UPDATE workspace_operational_states SET suspended_by_operator_id='operator-one'",
    "UPDATE workspace_operational_states SET suspended_at='2026-09-07T00:00:00Z'",
    "UPDATE tenants SET status='disabled'",
    "UPDATE workspaces SET status='migration_conflict'",
    "UPDATE workspace_tenant_bindings SET status='migration_conflict'",
    "UPDATE tenant_data_states SET lifecycle_status='deletion_pending'",
])
def test_incomplete_or_changed_new_states_are_not_reactivated(connection, change):
    connection.execute(change)
    before = connection.total_changes
    _ensure_workspace_operational_states(connection, "tenant-one", initialize_new_tenant=True)
    assert connection.total_changes == before


def test_tenant_created_flag_is_based_on_the_insert_result():
    with closing(sqlite3.connect(":memory:")) as database:
        database.execute("""CREATE TABLE tenants(
            tenant_id TEXT PRIMARY KEY,slug TEXT UNIQUE,name TEXT,service_mode TEXT,
            status TEXT,created_at TEXT,updated_at TEXT)""")
        tenant = {"slug": "example", "name": "Example"}
        assert _upsert_tenant(database, "tenant-one", tenant) is True
        assert _upsert_tenant(database, "tenant-one", {**tenant, "name": "Updated"}) is False
        assert database.execute("SELECT name FROM tenants").fetchone()[0] == "Updated"
        assert database.in_transaction
        database.rollback()
        assert database.execute("SELECT count(*) FROM tenants").fetchone()[0] == 0
