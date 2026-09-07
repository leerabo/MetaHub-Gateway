"""SQLite transaction lifecycle only; authentication is covered by real HTTP tests."""

import sqlite3
from contextlib import closing

import pytest
from metahub_control.auth.resource_transactions import ResourceWriteTransaction
from metahub_shared.runtime.errors import ApiError


@pytest.mark.parametrize("phase", ["begin", "body", "reauth", "commit", "success"])
def test_resource_transaction_closes_and_rolls_back_uncommitted_changes(
    tmp_path, monkeypatch, phase
):
    database = tmp_path / "lifecycle.sqlite3"
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE facts(value INTEGER NOT NULL)")
    authentication_calls = 0
    context_marker = object()

    def authenticate(transaction):
        nonlocal authentication_calls
        assert transaction.connection.in_transaction
        authentication_calls += 1
        if phase == "begin" or (phase == "reauth" and authentication_calls == 2):
            raise ApiError("auth.permission_denied")
        return context_marker

    def fail_commit(_connection):
        raise sqlite3.OperationalError("test commit failure")

    monkeypatch.setattr(ResourceWriteTransaction, "_authenticate", authenticate)

    def operation():
        with ResourceWriteTransaction(connection, None, context_marker, "project.write") as tx:
            assert tx.context is context_marker
            connection.execute("INSERT INTO facts VALUES(1)")
            if phase == "body":
                raise ValueError("test body failure")
            tx.commit(fail_commit if phase == "commit" else None)

    if phase in {"begin", "reauth"}:
        with pytest.raises(ApiError):
            operation()
    elif phase == "body":
        with pytest.raises(ValueError, match="test body failure"):
            operation()
    elif phase == "commit":
        with pytest.raises(sqlite3.OperationalError, match="test commit failure"):
            operation()
    else:
        operation()
    assert authentication_calls == (1 if phase in {"begin", "body"} else 2)
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connection.execute("SELECT 1")
    with closing(sqlite3.connect(database)) as check:
        assert check.execute("SELECT value FROM facts").fetchall() == (
            [(1,)] if phase == "success" else []
        )


def test_resource_transaction_rejects_unknown_permission_and_closes():
    connection = sqlite3.connect(":memory:")
    with pytest.raises(ApiError) as rejected:
        with ResourceWriteTransaction(connection, None, None, "other.write"):
            pytest.fail("unknown permission entered resource transaction")
    assert rejected.value.code == "infra.service_unavailable"
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connection.execute("SELECT 1")


def test_resource_transaction_refuses_commit_without_transaction():
    with closing(sqlite3.connect(":memory:")) as connection:
        transaction = ResourceWriteTransaction(connection, None, None, "project.write")
        with pytest.raises(ApiError) as rejected:
            transaction.commit()
        assert rejected.value.code == "infra.service_unavailable"
