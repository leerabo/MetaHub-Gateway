"""Real SQLite lock admission; full billing facts are verified by integration tests."""

from contextlib import closing
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from metahub_control.platform_billing import service
from metahub_shared.db import connect_database
from metahub_shared.db.connection import database_request_scope
from metahub_shared.runtime.errors import ApiError

NOW = datetime(2026, 9, 9, 4, tzinfo=UTC)
STAMP = service._db_time(NOW)


@pytest.fixture
def config(tmp_path, monkeypatch):
    current = SimpleNamespace(database_url=f"sqlite:///{(tmp_path / 'meter.sqlite3').as_posix()}")
    with closing(connect_database(current.database_url)) as connection:
        connection.executescript("""
            CREATE TABLE platform_meter_checkpoints(
                projection_code TEXT PRIMARY KEY, source_sequence INTEGER NOT NULL,
                source_fact_id TEXT, updated_at TEXT NOT NULL);
            CREATE TABLE usage_export_ledger_sequences(
                usage_export_sequence INTEGER PRIMARY KEY, usage_ledger_id TEXT NOT NULL);
            CREATE TABLE usage_ledger(usage_ledger_id TEXT PRIMARY KEY,
                tenant_id TEXT, request_id TEXT, input_tokens INTEGER,
                output_tokens INTEGER, total_tokens INTEGER, created_at TEXT);
            CREATE TABLE platform_meter_outbox(meter_outbox_id TEXT PRIMARY KEY,
                source_usage_ledger_id TEXT, status TEXT, attempt_count INTEGER DEFAULT 0,
                lease_owner_id TEXT, lease_token TEXT, lease_expires_at TEXT,
                error_code TEXT, updated_at TEXT);
            CREATE TABLE platform_billing_cycles(billing_cycle_id TEXT PRIMARY KEY,
                status TEXT, period_ends_at TEXT);
            CREATE TABLE platform_meter_definitions(id TEXT);
            CREATE TABLE platform_meter_readings(id TEXT);
            CREATE TABLE platform_quota_allocations(id TEXT);
            CREATE TABLE platform_billing_summaries(id TEXT);
            CREATE TABLE platform_billing_adjustments(id TEXT);
        """)
        connection.execute(
            "INSERT INTO platform_meter_checkpoints VALUES('usage_ledger_v1',0,NULL,?)", (STAMP,),
        )
        connection.commit()
    monkeypatch.setattr(service, "_utcnow", lambda: NOW)
    return current


def _usage(config, *, sequence=1, status="pending"):
    with closing(connect_database(config.database_url)) as connection:
        connection.execute("INSERT INTO usage_ledger VALUES('usage','tenant','request',1,2,3,?)",
                           (STAMP,))
        connection.execute("INSERT INTO usage_export_ledger_sequences VALUES(?,'usage')",
                           (sequence,))
        if status is not None:
            connection.execute("INSERT INTO platform_meter_outbox(meter_outbox_id,"
                               "source_usage_ledger_id,status) VALUES('outbox','usage',?)",
                               (status,))
        connection.commit()


def _operation(config, operation):
    return (service.project_usage_once(config, worker_id="idle-test") if operation == "project"
            else service.close_due_cycles(config))


@pytest.mark.parametrize("case,expected", [
    ("empty", None), ("failed", "platform_metering.reconciliation_required"),
    ("gap", "platform_metering.checkpoint_conflict"),
    ("missing-outbox", "platform_metering.checkpoint_conflict"),
    ("processing", "platform_metering.checkpoint_conflict"),
    ("no-cycle", None), ("future-cycle", None),
])
def test_idle_or_blocked_projection_never_requests_writer(config, monkeypatch, case, expected):
    operation = "close" if case.endswith("cycle") else "project"
    if case in {"failed", "gap", "missing-outbox", "processing"}:
        _usage(config, sequence=2 if case == "gap" else 1,
               status=None if case == "missing-outbox" else (
                   case if case in {"failed", "processing"} else "pending"))
    if case == "future-cycle":
        with closing(connect_database(config.database_url)) as connection:
            connection.execute("INSERT INTO platform_billing_cycles VALUES('future','open',?)",
                               (service._db_time(NOW + timedelta(days=1)),))
            connection.commit()
    statements = []
    original_connect = service._connect

    def observed(current):
        connection = original_connect(current)
        connection.execute("PRAGMA busy_timeout=0")
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(service, "_connect", observed)
    with closing(connect_database(config.database_url)) as writer:
        writer.execute("BEGIN IMMEDIATE")
        try:
            with database_request_scope(reuse_connections=True):
                if expected is None:
                    assert not _operation(config, operation)
                else:
                    with pytest.raises(ApiError) as error:
                        _operation(config, operation)
                    assert error.value.code == expected
                assert not connect_database(config.database_url).in_transaction
        finally:
            writer.rollback()
    assert "BEGIN IMMEDIATE" not in statements
    assert not any(sql.startswith(("UPDATE", "INSERT")) for sql in statements)


@pytest.mark.parametrize("operation", ["project", "close"])
def test_billing_admission_preserves_callers_transaction(config, operation):
    with database_request_scope(reuse_connections=True):
        connection = connect_database(config.database_url)
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("UPDATE platform_meter_checkpoints SET source_sequence=17")
        with pytest.raises(ApiError):
            _operation(config, operation)
        assert connection.in_transaction
        assert connection.execute(
            "SELECT source_sequence FROM platform_meter_checkpoints",
        ).fetchone()[0] == 17
        connection.rollback()


class _BeforeWrite:
    def __init__(self, connection, action):
        self.connection, self.action = connection, action

    def __getattr__(self, name):
        return getattr(self.connection, name)

    def execute(self, sql, *args):
        if sql == "BEGIN IMMEDIATE":
            assert not self.connection.in_transaction
            self.action()
        return self.connection.execute(sql, *args)


@pytest.mark.parametrize("change", ["completed", "failed"])
def test_projection_reloads_checkpoint_and_status_after_preflight(config, monkeypatch, change):
    _usage(config)
    visits = []

    def concurrent_writer():
        with closing(connect_database(config.database_url)) as connection:
            connection.execute("UPDATE platform_meter_outbox SET status=?", (change,))
            if change == "completed":
                connection.execute("UPDATE platform_meter_checkpoints SET source_sequence=1")
            connection.commit()
        visits.append("writer")

    original_connect = service._connect
    monkeypatch.setattr(service, "_connect", lambda current:
                        _BeforeWrite(original_connect(current), concurrent_writer))
    if change == "completed":
        assert not service.project_usage_once(config, worker_id="late-reader")
    else:
        with pytest.raises(ApiError) as error:
            service.project_usage_once(config, worker_id="late-reader")
        assert error.value.code == "platform_metering.reconciliation_required"
    assert visits == ["writer"]
    with closing(connect_database(config.database_url)) as connection:
        assert connection.execute(
            "SELECT attempt_count FROM platform_meter_outbox",
        ).fetchone()[0] == 0


def test_cycle_close_reloads_due_rows_and_time(config, monkeypatch):
    clock = [NOW]
    monkeypatch.setattr(service, "_utcnow", lambda: clock[0])
    with closing(connect_database(config.database_url)) as connection:
        connection.executemany("INSERT INTO platform_billing_cycles VALUES(?,'open',?)", [
            ("first", STAMP), ("second", service._db_time(NOW + timedelta(seconds=1))),
        ])
        connection.commit()

    def concurrent_writer():
        clock[0] += timedelta(seconds=2)
        with closing(connect_database(config.database_url)) as connection:
            connection.execute("UPDATE platform_billing_cycles SET status='closed' "
                               "WHERE billing_cycle_id='first'")
            connection.commit()

    closed = []

    def close(connection, row, *, now):
        assert connection.in_transaction and now == clock[0]
        closed.append(row["billing_cycle_id"])

    original_connect = service._connect
    monkeypatch.setattr(service, "_connect", lambda current:
                        _BeforeWrite(original_connect(current), concurrent_writer))
    monkeypatch.setattr(service, "_close_cycle", close)
    assert service.close_due_cycles(config) == 1
    assert closed == ["second"]


@pytest.mark.parametrize("operation", ["project", "close"])
def test_billing_preflight_schema_failure_stays_unavailable(config, operation):
    with closing(connect_database(config.database_url)) as connection:
        connection.execute("DROP TABLE platform_meter_definitions")
        connection.commit()
    with database_request_scope(reuse_connections=True):
        with pytest.raises(ApiError) as error:
            _operation(config, operation)
        assert error.value.code == "platform_billing.unavailable"
        assert not connect_database(config.database_url).in_transaction
