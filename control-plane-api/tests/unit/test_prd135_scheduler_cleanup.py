"""Real transaction cleanup when the outer scheduler fence rejects lease release."""

import sqlite3
from types import SimpleNamespace

import pytest
from metahub_control.compliance_center import service
from metahub_shared.db import connect_database
from metahub_shared.db.connection import database_commit_guard
from metahub_shared.runtime.errors import ApiError


@pytest.mark.parametrize("reject_commit", [False, True])
def test_lease_release_always_closes_and_releases_the_write_lock(tmp_path, monkeypatch, reject_commit):
    config = SimpleNamespace(database_url=f"sqlite:///{(tmp_path / 'cleanup.sqlite3').as_posix()}")
    connection = connect_database(config.database_url)
    connection.execute("""CREATE TABLE compliance_scheduler_state(
        singleton_id INTEGER PRIMARY KEY, lease_owner_id TEXT, lease_token_sha256 TEXT,
        fencing_generation INTEGER, lease_expires_at TEXT, revision INTEGER, updated_at TEXT)""")
    connection.execute("""INSERT INTO compliance_scheduler_state VALUES(
        1,'worker','digest',1,'2099-01-01T00:00:00Z',1,'2020-01-01T00:00:00Z')""")
    connection.commit()
    monkeypatch.setattr(service, "_connect", lambda _: connection)

    def guard(_connection):
        if reject_commit:
            raise ApiError("platform_job.lease_lost")

    with database_commit_guard(guard):
        service._release_lease(config, "worker", 1, "digest")
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connection.execute("SELECT 1")

    observer = connect_database(config.database_url)
    try:
        observer.execute("PRAGMA busy_timeout=20")
        observer.execute("BEGIN IMMEDIATE")
        row = observer.execute("SELECT lease_owner_id,revision FROM compliance_scheduler_state").fetchone()
        assert tuple(row) == (("worker", 1) if reject_commit else (None, 2))
    finally:
        observer.rollback()
        observer.close()


def test_lease_release_remains_best_effort_when_connecting_fails(monkeypatch):
    def fail(_config):
        raise ApiError("compliance.unavailable")

    monkeypatch.setattr(service, "_connect", fail)
    service._release_lease(None, "worker", 1, "digest")


def test_lease_release_closes_even_when_rollback_fails(monkeypatch):
    closed = []

    class Connection:
        in_transaction = True

        def execute(self, _sql):
            raise sqlite3.OperationalError("controlled failure")

        def rollback(self):
            raise sqlite3.OperationalError("controlled rollback failure")

        def close(self):
            closed.append(True)

    monkeypatch.setattr(service, "_connect", lambda _: Connection())
    service._release_lease(None, "worker", 1, "digest")
    assert closed == [True]
