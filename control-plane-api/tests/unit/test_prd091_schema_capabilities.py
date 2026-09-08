"""Identity capability checks retain live MFA facts and caller transactions."""

import sqlite3
from contextlib import closing

import pytest
from metahub_control.identity import service


def test_missing_mfa_capability_retains_legacy_projection():
    with closing(sqlite3.connect(":memory:")) as connection:
        assert not service._mfa_configured(connection, "account", "not_enrolled")
        assert service._mfa_configured(connection, "account", "enrolled")


def test_present_mfa_capability_uses_only_current_account_active_facts():
    with closing(sqlite3.connect(":memory:")) as connection:
        connection.execute("CREATE TABLE account_mfa_factors(account_id TEXT,status TEXT)")
        assert not service._mfa_configured(connection, "account", "enrolled")
        connection.executemany(
            "INSERT INTO account_mfa_factors VALUES(?,?)",
            [
                ("account", "revoked"),
                ("another-account", "active"),
            ],
        )
        assert connection.in_transaction
        assert not service._mfa_configured(connection, "account", "enrolled")
        connection.execute("INSERT INTO account_mfa_factors VALUES('account','active')")
        assert service._mfa_configured(connection, "account", "not_enrolled")
        assert connection.in_transaction
        connection.rollback()
        assert not service._mfa_configured(connection, "account", "enrolled")


def test_identity_schema_helpers_do_not_accept_views_or_change_transaction():
    with closing(sqlite3.connect(":memory:")) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute(
            "CREATE TABLE global_sessions(session_id TEXT,revoke_generation INTEGER)"
        )
        connection.execute("CREATE VIEW platform_operators AS SELECT * FROM global_sessions")
        connection.execute("BEGIN IMMEDIATE")
        assert service._table_exists(connection, "global_sessions")
        assert not service._table_exists(connection, "platform_operators")
        assert service._column_exists(connection, "global_sessions", "revoke_generation")
        assert not service._column_exists(connection, "global_sessions", "unknown")
        assert not service._column_exists(connection, "missing", "revoke_generation")
        with pytest.raises(ValueError):
            service._table_exists(connection, "bad;table")
        assert connection.in_transaction
