"""Opt-in PostgreSQL deadlines retain one absolute budget and clean up context."""

import hashlib
import sqlite3
from contextlib import nullcontext
from types import SimpleNamespace

import pytest
from metahub_shared.db import connection as database


class NativeConnection:
    def __init__(self):
        self.statements = []
        self.info = SimpleNamespace(transaction_status=SimpleNamespace(name="IDLE"))
        self.rollbacks = 0

    def cursor(self):
        owner = self

        class Cursor:
            description = None

            def execute(self, sql, params=None, **kwargs):
                owner.statements.append((sql, params))

            def close(self):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *_):
                pass

        return Cursor()

    def pipeline(self):
        return nullcontext()

    def rollback(self):
        self.rollbacks += 1


def test_deadline_recomputes_remaining_budget_and_restores_outer_context(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(database.time, "monotonic", lambda: now[0])
    native = NativeConnection()
    connection = database._PostgresConnection(native, None)
    with database.database_operation_deadline(100.250):
        connection.execute("SELECT 1")
        now[0] += 0.050
        with database.database_operation_deadline(200):
            connection.execute("SELECT 2")
        with database.database_operation_deadline(100.080):
            connection.execute("SELECT 3")
        connection.execute("SELECT 4")
    settings = [params for sql, params in native.statements if "set_config" in sql]
    assert [int(params[0]) for params in settings] == [250, 200, 30, 200]
    assert all(params[0] == params[1] for params in settings)
    assert database._DATABASE_OPERATION_DEADLINE.get() is None
    size = len(native.statements)
    connection.execute("SELECT 5")
    assert len(native.statements) == size + 1


def test_expired_deadline_blocks_sql_but_never_blocks_rollback(monkeypatch):
    monkeypatch.setattr(database.time, "monotonic", lambda: 100)
    native = NativeConnection()
    connection = database._PostgresConnection(native, None)
    with database.database_operation_deadline(99):
        with pytest.raises(sqlite3.Error):
            connection.execute("SELECT 1")
        assert native.statements == []
        connection.rollback()
        connection.execute("ROLLBACK")
    assert native.rollbacks == 1
    assert native.statements == [("ROLLBACK", None)]


def test_read_only_snapshot_starts_before_transaction_local_deadline_settings(monkeypatch):
    """A settings SELECT would implicitly start a transaction before isolation is set."""
    monkeypatch.setattr(database.time, "monotonic", lambda: 100)
    native = NativeConnection()
    connection = database._PostgresConnection(native, None)
    begin = "BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY"
    with database.database_operation_deadline(101):
        connection.execute(begin)
        connection.execute("SELECT 1")
        connection.execute("ROLLBACK")
    assert native.statements[0] == (begin, None)
    assert "set_config" in native.statements[1][0]
    assert native.statements[2:] == [("SELECT 1", None), ("ROLLBACK", None)]


@pytest.mark.parametrize("deadline", [True, "bad", float("nan"), float("inf")])
def test_invalid_deadline_does_not_change_context(deadline):
    with pytest.raises(sqlite3.Error), database.database_operation_deadline(deadline):
        pytest.fail("Invalid deadline entered the operation")
    assert database._DATABASE_OPERATION_DEADLINE.get() is None


def test_postgresql_checkout_uses_remaining_budget_and_skips_expired_pool(monkeypatch):
    url = "postgresql://deadline-test.invalid/test"
    calls = []
    pool = SimpleNamespace(getconn=lambda **kwargs: calls.append(kwargs) or NativeConnection())
    monkeypatch.setitem(database._POSTGRES_POOLS, hashlib.sha256(url.encode()).hexdigest(), pool)
    monkeypatch.setattr(database.time, "monotonic", lambda: 100)
    with database.database_operation_deadline(100.060):
        database._connect_postgresql(url)
    assert 0 < calls[0]["timeout"] <= 0.0600001
    with pytest.raises(database.DbError), database.database_operation_deadline(99):
        database._connect_postgresql(url)
    assert len(calls) == 1


def test_postgresql_feedback_sum_keeps_arbitrary_precision_without_coercing_floats():
    from metahub_shared.feedback.storage import _postgres_summary_aggregates, _strict_int

    result = _postgres_summary_aggregates({"rating_sum": "18446744073709551615",
                                          "accepted_count": "0"})
    assert result["rating_sum"] == 18446744073709551615
    assert result["accepted_count"] == 0
    for invalid in (True, 1.5, "1.0", "1e2", "-1", " 1", ""):
        value = _postgres_summary_aggregates({"rating_sum": invalid, "accepted_count": "0"})
        with pytest.raises(ValueError):
            _strict_int(value["rating_sum"])
