"""Budget reads enforce database-native read-only transactions."""

import sqlite3
from contextlib import closing

import pytest
from metahub_control.budgets.service import _begin_read_transaction


def test_budget_sqlite_read_transaction_rejects_writes():
    with closing(sqlite3.connect(":memory:")) as connection:
        connection.execute("CREATE TABLE facts(value INTEGER)")
        _begin_read_transaction(connection)
        assert connection.in_transaction
        assert connection.execute("SELECT count(*) FROM facts").fetchone()[0] == 0
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            connection.execute("INSERT INTO facts VALUES(1)")
        connection.rollback()


def test_budget_postgresql_read_transaction_does_not_send_sqlite_pragma():
    class Connection:
        backend = "postgresql"

        def __init__(self):
            self.statements = []

        def execute(self, sql):
            self.statements.append(sql)

    connection = Connection()
    _begin_read_transaction(connection)
    assert connection.statements == ["BEGIN", "SET TRANSACTION READ ONLY"]
