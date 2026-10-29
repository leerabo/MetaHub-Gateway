"""Workspace capability checks use existing schema and transaction ownership."""

import sqlite3
from contextlib import closing

import pytest
from metahub_control.workspaces import service


def test_operational_capability_requires_real_table_and_keeps_transaction():
    with closing(sqlite3.connect(":memory:")) as connection:
        assert not service._table_exists(connection, "workspace_operational_states")
        connection.execute("CREATE TABLE workspace_operational_states(workspace_id TEXT)")
        connection.execute("CREATE VIEW view_only AS SELECT * FROM workspace_operational_states")
        connection.execute("BEGIN IMMEDIATE")
        assert service._table_exists(connection, "workspace_operational_states")
        assert not service._table_exists(connection, "view_only")
        assert connection.in_transaction


@pytest.fixture
def operational_connection():
    with closing(sqlite3.connect(":memory:")) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("""CREATE TABLE workspace_operational_states(
            workspace_operational_state_id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL UNIQUE,tenant_id TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL CHECK(status IN('active','suspended')),
            revision INTEGER NOT NULL,risk_revision INTEGER NOT NULL,source TEXT NOT NULL,
            suspended_reason_code TEXT,suspended_by_operator_id TEXT,suspended_at TEXT,
            created_at TEXT NOT NULL,updated_at TEXT NOT NULL
        )""")
        yield connection


def test_missing_operational_table_keeps_historical_schema_transaction():
    with closing(sqlite3.connect(":memory:")) as connection:
        connection.execute("BEGIN IMMEDIATE")
        service._initialize_operational_state(connection, "workspace", "tenant", "stamp")
        assert connection.in_transaction


def test_initialization_reuses_same_owner_without_changing_row_and_can_roll_back(
    operational_connection,
):
    connection = operational_connection
    service._initialize_operational_state(connection, "workspace", "tenant", "original")
    before = tuple(connection.execute("SELECT * FROM workspace_operational_states").fetchone())
    service._initialize_operational_state(connection, "workspace", "tenant", "later")
    assert (
        tuple(connection.execute("SELECT * FROM workspace_operational_states").fetchone()) == before
    )
    assert connection.in_transaction
    connection.rollback()
    assert (
        connection.execute("SELECT COUNT(*) FROM workspace_operational_states").fetchone()[0] == 0
    )


@pytest.mark.parametrize("invalid", ["owner", "state"])
def test_existing_wrong_owner_or_inactive_state_is_never_reset(operational_connection, invalid):
    connection = operational_connection
    service._initialize_operational_state(connection, "workspace", "tenant", "original")
    if invalid == "state":
        connection.execute("UPDATE workspace_operational_states SET status='suspended'")
    connection.commit()
    before = tuple(connection.execute("SELECT * FROM workspace_operational_states").fetchone())
    with pytest.raises(sqlite3.IntegrityError, match="workspace_operational_state_invalid"):
        service._initialize_operational_state(
            connection,
            "workspace",
            "foreign" if invalid == "owner" else "tenant",
            "later",
        )
    connection.rollback()
    assert (
        tuple(connection.execute("SELECT * FROM workspace_operational_states").fetchone()) == before
    )


@pytest.mark.parametrize("invalid", ["tenant_unique", "primary_key", "not_null"])
def test_non_workspace_constraints_are_not_ignored(operational_connection, monkeypatch, invalid):
    connection = operational_connection
    service._initialize_operational_state(connection, "workspace", "tenant", "original")
    existing_id = connection.execute(
        "SELECT workspace_operational_state_id FROM workspace_operational_states",
    ).fetchone()[0]
    if invalid == "primary_key":
        monkeypatch.setattr(service, "_id", lambda _prefix: existing_id)
    connection.commit()
    with pytest.raises(sqlite3.IntegrityError):
        service._initialize_operational_state(
            connection,
            "another-workspace",
            "tenant" if invalid == "tenant_unique" else "another-tenant",
            None if invalid == "not_null" else "later",
        )
    connection.rollback()
    assert (
        connection.execute("SELECT COUNT(*) FROM workspace_operational_states").fetchone()[0] == 1
    )
