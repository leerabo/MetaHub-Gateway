"""Full integrity maintenance must not run at the business polling cadence."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from metahub_control.console_experience import scheduler
from metahub_shared.db import connect_database


@pytest.mark.parametrize("kind,worked,delay", [
    ("reconciliation", False, 300),
    ("workspace_dashboard_refresh", False, 2),
    ("workspace_outbox", True, 1),
])
def test_persisted_next_attempt_separates_maintenance_from_business(
    tmp_path, monkeypatch, kind, worked, delay
):
    config = SimpleNamespace(
        database_url=f"sqlite:///{(tmp_path / 'cadence.sqlite3').as_posix()}",
        workspace_session_secret="test-cadence-secret",
    )
    now = datetime(2026, 9, 8, tzinfo=UTC)
    monkeypatch.setattr(scheduler, "_now", lambda: now)
    connection = connect_database(config.database_url)
    connection.execute("""CREATE TABLE console_experience_scheduler_state(
        worker_kind TEXT PRIMARY KEY,enabled INTEGER,fence_generation INTEGER,
        lease_owner_id TEXT,lease_token_hmac TEXT,lease_expires_at TEXT,attempt_count INTEGER,
        next_attempt_at TEXT,cursor_value TEXT,last_error_code TEXT,updated_at TEXT)""")
    stamp = scheduler._db_time(now)
    connection.execute(
        """INSERT INTO console_experience_scheduler_state
           VALUES(?,1,1,NULL,NULL,NULL,0,?,NULL,NULL,?)""",
        (kind, stamp, stamp),
    )
    connection.commit()
    connection.close()
    lease = scheduler._claim(config, kind)
    assert lease is not None
    scheduler._release(config, lease, worked=worked)
    assert not bool(scheduler._claim(config, kind))
    now += timedelta(seconds=delay - 1)
    assert not bool(scheduler._claim(config, kind))
    now += timedelta(seconds=1)
    assert scheduler._claim(config, kind) is not None


def test_reconciliation_checks_foreign_keys_and_one_integrity_table(tmp_path, monkeypatch):
    config = SimpleNamespace(
        database_url=f"sqlite:///{(tmp_path / 'integrity.sqlite3').as_posix()}"
    )
    connection = connect_database(config.database_url)
    statements = []
    connection.set_trace_callback(statements.append)
    monkeypatch.setattr(scheduler, "_connect", lambda _config: connection)
    assert scheduler._run_worker(config, "reconciliation") is False
    assert "PRAGMA foreign_key_check" in statements
    assert any(statement.startswith("PRAGMA integrity_check") for statement in statements)


def test_integrity_cursor_rotates_across_tables(tmp_path, monkeypatch):
    config = SimpleNamespace(
        database_url=f"sqlite:///{(tmp_path / 'rotation.sqlite3').as_posix()}"
    )
    connection = connect_database(config.database_url)
    connection.execute("CREATE TABLE first_probe (id INTEGER PRIMARY KEY)")
    connection.execute("CREATE TABLE second_probe (id INTEGER PRIMARY KEY)")
    connection.commit()
    connection.close()
    scheduler._INTEGRITY_TABLE_CURSOR.pop(config.database_url, None)
    monkeypatch.setattr(scheduler, "INTEGRITY_TABLES_PER_PASS", 1)

    scheduler._verify_integrity(config)
    scheduler._verify_integrity(config)

    assert scheduler._INTEGRITY_TABLE_CURSOR[config.database_url] == 0
