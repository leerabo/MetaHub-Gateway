"""Console idle polls avoid SQLite writers without weakening claim fences."""

from contextlib import closing
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from metahub_control.console_experience import scheduler
from metahub_shared.db import connect_database
from metahub_shared.db.connection import database_request_scope
from metahub_shared.runtime.errors import ApiError

NOW = datetime(2026, 9, 9, 4, tzinfo=UTC)
STAMP = scheduler._db_time(NOW)
FUTURE = scheduler._db_time(NOW + timedelta(minutes=5))


@pytest.fixture(params=["console", "navigation"])
def claim_case(request, tmp_path, monkeypatch):
    navigation = request.param == "navigation"
    table = ("workspace_navigation_scheduler_state" if navigation
             else "console_experience_scheduler_state")
    column = "queue_code" if navigation else "worker_kind"
    config = SimpleNamespace(
        database_url=f"sqlite:///{(tmp_path / 'claims.sqlite3').as_posix()}",
        workspace_session_secret="idle-claim-test-only-" + "x" * 32,
    )
    with closing(connect_database(config.database_url)) as connection:
        connection.execute(f"""CREATE TABLE {table}(
            {column} TEXT PRIMARY KEY, enabled INTEGER NOT NULL,
            next_attempt_at TEXT NOT NULL, lease_expires_at TEXT,
            fence_generation INTEGER NOT NULL, lease_owner_id TEXT,
            lease_token_hmac TEXT, attempt_count INTEGER NOT NULL,
            updated_at TEXT NOT NULL)""")
        connection.execute(
            f"INSERT INTO {table} VALUES(?,1,?,NULL,4,NULL,NULL,0,?)",
            ("cleanup", STAMP, STAMP),
        )
        connection.commit()
    monkeypatch.setattr(scheduler, "_now", lambda: NOW)
    claim = scheduler._claim_navigation if navigation else scheduler._claim
    return SimpleNamespace(config=config, table=table, column=column, claim=claim)


def _change(case, assignments, parameters=()):
    with closing(connect_database(case.config.database_url)) as connection:
        connection.execute(
            f"UPDATE {case.table} SET {assignments}", parameters,
        )
        connection.commit()


def _row(case):
    with closing(connect_database(case.config.database_url)) as connection:
        row = connection.execute(f"SELECT * FROM {case.table}").fetchone()
        return dict(row) if row is not None else None


@pytest.mark.parametrize("reason", ["missing", "disabled", "cooldown", "leased"])
def test_idle_claim_does_not_request_writer_lock(claim_case, monkeypatch, reason):
    case = claim_case
    if reason == "missing":
        with closing(connect_database(case.config.database_url)) as connection:
            connection.execute(f"DELETE FROM {case.table}")
            connection.commit()
    elif reason == "disabled":
        _change(case, "enabled=0")
    elif reason == "cooldown":
        _change(case, "next_attempt_at=?", (FUTURE,))
    else:
        _change(case, "lease_expires_at=?", (FUTURE,))
    before = _row(case)
    statements = []
    original_connect = scheduler._connect

    def observed_connect(config):
        connection = original_connect(config)
        connection.execute("PRAGMA busy_timeout=0")
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(scheduler, "_connect", observed_connect)
    with closing(connect_database(case.config.database_url)) as writer:
        writer.execute("BEGIN IMMEDIATE")
        try:
            with database_request_scope(reuse_connections=True):
                assert case.claim(case.config, "cleanup") is None
                assert not connect_database(case.config.database_url).in_transaction
        finally:
            writer.rollback()
    assert not any(sql.startswith("BEGIN IMMEDIATE") for sql in statements)
    assert not any(sql.startswith("UPDATE") for sql in statements)
    assert _row(case) == before


class _BeforeWrite:
    def __init__(self, connection, action):
        self.connection = connection
        self.action = action

    def __getattr__(self, name):
        return getattr(self.connection, name)

    def execute(self, sql, *args):
        if sql == "BEGIN IMMEDIATE":
            assert not self.connection.in_transaction
            self.action()
        return self.connection.execute(sql, *args)


@pytest.mark.parametrize("change", ["disabled", "cooldown", "leased", "deleted"])
def test_claim_rechecks_concurrent_ineligibility(claim_case, monkeypatch, change):
    case = claim_case
    changed = []

    def concurrent_change():
        # Open outside a request scope so this is a real independent writer.
        with closing(connect_database(case.config.database_url)) as connection:
            if change == "deleted":
                connection.execute(f"DELETE FROM {case.table}")
            else:
                assignment = {
                    "disabled": "enabled=0", "cooldown": "next_attempt_at=?",
                    "leased": "lease_expires_at=?",
                }[change]
                connection.execute(f"UPDATE {case.table} SET {assignment}",
                                   () if change == "disabled" else (FUTURE,))
            connection.commit()
        changed.append(_row(case))

    original_connect = scheduler._connect
    monkeypatch.setattr(scheduler, "_connect", lambda config:
                        _BeforeWrite(original_connect(config), concurrent_change))
    assert case.claim(case.config, "cleanup") is None
    assert len(changed) == 1
    assert _row(case) == changed[0]


def test_claim_uses_fresh_time_and_generation(claim_case, monkeypatch):
    case = claim_case
    _change(case, "lease_expires_at=?", (STAMP,))
    clock = [NOW]
    monkeypatch.setattr(scheduler, "_now", lambda: clock[0])

    def concurrent_change():
        clock[0] += timedelta(seconds=10)
        _change(case, "fence_generation=9")

    original_connect = scheduler._connect
    monkeypatch.setattr(scheduler, "_connect", lambda config:
                        _BeforeWrite(original_connect(config), concurrent_change))
    lease = case.claim(case.config, "cleanup")
    assert lease is not None and lease.generation == 10
    row = _row(case)
    assert row["fence_generation"] == 10 and row["attempt_count"] == 1
    assert row["updated_at"] == scheduler._db_time(clock[0])
    assert row["lease_expires_at"] == scheduler._db_time(
        clock[0] + timedelta(seconds=scheduler.LEASE_SECONDS),
    )
    hmac = (scheduler._navigation_lease_hmac if case.claim == scheduler._claim_navigation
            else scheduler._lease_hmac)
    assert row["lease_token_hmac"] == hmac(case.config, "cleanup", lease.token)
    assert case.claim(case.config, "cleanup") is None


def test_claim_preserves_callers_active_transaction(claim_case):
    case = claim_case
    with database_request_scope(reuse_connections=True):
        connection = connect_database(case.config.database_url)
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(f"UPDATE {case.table} SET attempt_count=17")
        with pytest.raises(ApiError) as error:
            case.claim(case.config, "cleanup")
        assert error.value.code == "platform.unavailable"
        assert connection.in_transaction
        assert connection.execute(
            f"SELECT attempt_count FROM {case.table}",
        ).fetchone()[0] == 17
        connection.rollback()
    assert _row(case)["attempt_count"] == 0


def test_claim_read_failure_is_not_reported_as_idle(claim_case):
    case = claim_case
    with closing(connect_database(case.config.database_url)) as connection:
        connection.execute(f"DROP TABLE {case.table}")
        connection.commit()
    with database_request_scope(reuse_connections=True):
        with pytest.raises(ApiError) as error:
            case.claim(case.config, "cleanup")
        assert error.value.code == "platform.unavailable"
        assert not connect_database(case.config.database_url).in_transaction
